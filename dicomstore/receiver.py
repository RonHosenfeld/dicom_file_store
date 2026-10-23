"""DICOM C-STORE SCP: accepts all storage SOP classes, one thread per association."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

from pydicom.dataset import Dataset
from pydicom.uid import DeflatedExplicitVRLittleEndian
from pynetdicom import AE, evt, AllStoragePresentationContexts

from . import config as config_mod
from . import storage
from . import study_index

logger = logging.getLogger(__name__)

# Cannot understand the received dataset (decode failure, missing required tags).
STATUS_CANNOT_UNDERSTAND = 0xC210
# Out of resources (write failure, disk full).
STATUS_OUT_OF_RESOURCES = 0xA700


def handle_store(event, cfg: config_mod.Config) -> int:
    """Store one received instance. Must never raise; always returns a status."""
    try:
        ds = event.dataset
        # Strip any group-0002 elements that leaked into the dataset
        # (same pattern as pynetdicom's own apps/common.py).
        ds = ds[0x00030000:]
        ds.file_meta = event.file_meta

        def tag(keyword: str) -> str:
            value = getattr(ds, keyword, None)
            return str(value) if value is not None else ""

        sop_instance_uid = tag("SOPInstanceUID")
        sop_class_uid = tag("SOPClassUID")
        if not sop_instance_uid or not sop_class_uid:
            logger.error("Instance missing SOPInstanceUID/SOPClassUID; rejecting")
            return STATUS_CANNOT_UNDERSTAND

        folder = storage.study_folder(
            Path(cfg.storage_path), tag("AccessionNumber"), tag("StudyInstanceUID")
        )
        filename = storage.sanitize_component(sop_instance_uid) + ".dcm"
        target = folder / filename
        existed = target.exists()

        # pydicom cannot reliably re-write deflated datasets; store the exact
        # encoded bytes received instead.
        if event.context.transfer_syntax == DeflatedExplicitVRLittleEndian:
            # include_meta=True returns a complete Part-10 bytestream.
            storage.atomic_write_bytes(folder, filename, event.encoded_dataset())
        else:
            folder.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=folder, prefix=".tmp_", suffix=".part")
            try:
                with os.fdopen(fd, "wb") as f:
                    ds.save_as(f, enforce_file_format=True)
                os.replace(tmp, target)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

        study_index.update_from_instance(folder, ds, filename)

        calling_ae = event.assoc.requestor.ae_title
        logger.info(
            "%s instance from AE '%s': accession=%s file=%s%s",
            "Overwrote" if existed else "Stored",
            calling_ae,
            tag("AccessionNumber") or "<none>",
            f"{folder.name}/{filename}",
            " (duplicate)" if existed else "",
        )
        return 0x0000
    except Exception:
        logger.exception("Failed to store instance")
        return STATUS_OUT_OF_RESOURCES


class ReceiverManager:
    """Starts/stops the pynetdicom association server inside this process."""

    def __init__(self) -> None:
        self._server = None
        self._lock = threading.Lock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._server is not None

    def start(self, cfg: config_mod.Config) -> None:
        with self._lock:
            if self._server is not None:
                return
            ae = AE(ae_title=cfg.ae_title)
            # Accept every storage SOP class with every transfer syntax,
            # plus C-ECHO so senders can run connectivity checks.
            ae.supported_contexts = AllStoragePresentationContexts
            # Verification SOP Class UID (C-ECHO) — stable public DICOM UID.
            ae.add_supported_context("1.2.840.10008.1.1")
            self._server = ae.start_server(
                ("", cfg.port),
                block=False,
                evt_handlers=[(evt.EVT_C_STORE, self._on_store, [cfg])],
            )
            logger.info("Receiver listening on port %d as AE '%s', storing to %s",
                        cfg.port, cfg.ae_title, cfg.storage_path)

    def _on_store(self, event, cfg: config_mod.Config) -> int:
        return handle_store(event, cfg)

    def stop(self) -> None:
        with self._lock:
            if self._server is None:
                return
            server, self._server = self._server, None
        server.shutdown()
        logger.info("Receiver stopped")
