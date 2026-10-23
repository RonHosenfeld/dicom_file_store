"""C-STORE SCU send engine: series work units, concurrency, throttling.

Deliberately free of FastAPI/job-registry imports so the send loop is
unit-testable on its own. Workers consume a queue of series; each worker
holds one association (pynetdicom associations are not thread-safe and must
not be shared), opened lazily and reused across series.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.uid import ExplicitVRLittleEndian
from pynetdicom import AE, Association

from .generator import ExamPlan, SeriesSpec, iter_series_instances

logger = logging.getLogger(__name__)

STATUS_SUCCESS = 0x0000
# Generic failure reported when a status could not be determined at all.
STATUS_FAILURE = 0xC000
DEFAULT_ASSOCIATE_TIMEOUT = 10
DEFAULT_CALLING_AE = "DICOMSTORE-SCU"

STATUS_NAME = {
    0xFE00: "cancelled",
    0xA700: "out of resources",
    0xC000: "cannot understand",
    0xC210: "cannot understand (receiver)",
}


@dataclass
class SendTarget:
    host: str
    port: int
    called_ae: str
    calling_ae: str = DEFAULT_CALLING_AE


@dataclass
class SendRequest:
    target: SendTarget
    concurrency: int = 1
    delay_ms: int = 0
    max_per_second: int | None = None


@dataclass
class GeneratedSeries:
    """One series of lazily generated datasets from an ExamPlan."""
    plan: ExamPlan
    spec: SeriesSpec

    @property
    def label(self) -> str:
        return f"{self.spec.modality} series {self.spec.series_number}"

    @property
    def instance_count(self) -> int:
        return self.spec.image_count

    @property
    def sop_class_uid(self) -> str:
        return self.spec.sop_class_uid

    def iter_datasets(self) -> Iterator:
        return iter_series_instances(self.plan, self.spec)


@dataclass
class FileSeries:
    """One series of instances referenced by scanned file paths."""
    series_uid: str
    modality: str
    refs: list  # list[scanner.InstanceRef]

    @property
    def label(self) -> str:
        return f"{self.modality} series"

    @property
    def instance_count(self) -> int:
        return len(self.refs)

    def iter_datasets(self) -> Iterator:
        return _iter_file_datasets(self)


def series_sop_classes(item) -> set[str]:
    """SOP Class UIDs to propose for this work unit."""
    if isinstance(item, GeneratedSeries):
        return {item.spec.sop_class_uid}
    return {ref.sop_class_uid for ref in item.refs if ref.sop_class_uid}


def _pace(delay_seconds: float, max_per_second: int | None, concurrency: int) -> float:
    """Minimum interval between sends for one worker."""
    interval = delay_seconds
    if max_per_second and max_per_second > 0:
        interval = max(interval, 1.0 / (max_per_second / concurrency))
    return interval


def send_job(work_items: list, request: SendRequest, progress,
             cancel_event: threading.Event) -> None:
    """Send all work items, updating ``progress``. Blocks until done."""
    items = queue.Queue()
    for item in work_items:
        items.put(item)

    # Collect the distinct SOP classes so every worker proposes the same set
    # (associations are reused across series with potentially different classes).
    sop_classes: set[str] = set()
    for item in work_items:
        sop_classes |= series_sop_classes(item)

    if request.concurrency <= 1:
        _worker(items, request, progress, cancel_event, sop_classes)
        return

    threads = [
        threading.Thread(target=_worker, daemon=True,
                         args=(items, request, progress, cancel_event, sop_classes),
                         name=f"send-worker-{n}")
        for n in range(request.concurrency)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()


def _worker(items: queue.Queue, request: SendRequest, progress, cancel_event,
            sop_classes: set[str] | None = None) -> None:
    assoc: Association | None = None
    try:
        while True:
            if cancel_event.is_set():
                return
            try:
                item = items.get_nowait()
            except queue.Empty:
                return
            if assoc is None or not assoc.is_established:
                wanted = sop_classes if sop_classes is not None else series_sop_classes(item)
                assoc = _associate(request.target, wanted)
                if assoc is None:
                    logger.error("Association to %s:%d as '%s' failed",
                                 request.target.host, request.target.port,
                                 request.target.called_ae)
                    for _ in range(item.instance_count):
                        progress.record(STATUS_FAILURE, error="association failed")
                    progress.series_finished()
                    continue
            progress.series_started(item.label)
            try:
                _send_series(assoc, item, request, progress, cancel_event)
            finally:
                progress.series_finished()
    finally:
        if assoc is not None and assoc.is_established:
            assoc.release()


def _associate(target: SendTarget, sop_classes: set[str]) -> Association | None:
    ae = AE(ae_title=target.calling_ae)
    for sop_class in sop_classes:
        ae.add_requested_context(sop_class, ExplicitVRLittleEndian)
    assoc = ae.associate(target.host, target.port)
    assoc.acse_timeout = DEFAULT_ASSOCIATE_TIMEOUT
    assoc.network_timeout = DEFAULT_ASSOCIATE_TIMEOUT
    return assoc if assoc.is_established else None


def _send_series(assoc: Association, item, request: SendRequest, progress,
                 cancel_event: threading.Event) -> None:
    interval = _pace(request.delay_ms / 1000.0, request.max_per_second,
                     request.concurrency)
    for ds in item.iter_datasets():
        if cancel_event.is_set():
            return
        start = time.monotonic()
        status = assoc.send_c_store(ds)
        status_code = int(status.Status) if status is not None else STATUS_FAILURE
        if status_code == STATUS_SUCCESS:
            progress.record(status_code)
        else:
            progress.record(status_code,
                            error=STATUS_NAME.get(status_code, "C-STORE failed"))
        if interval > 0:
            remainder = interval - (time.monotonic() - start)
            if remainder > 0:
                time.sleep(remainder)


def _iter_file_datasets(item: FileSeries) -> Iterator:
    for ref in item.refs:
        try:
            yield pydicom.dcmread(ref.path)
        except (InvalidDicomError, OSError) as exc:
            logger.warning("Skipping unreadable file %s: %s", ref.path, exc)
