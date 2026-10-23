"""E2E tests for the send engine: generated + file-based sends into a real receiver."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from dicomstore import config as config_mod
from dicomstore.generator import build_plan, iter_instances
from dicomstore.jobs import SendProgress
from dicomstore.receiver import ReceiverManager
from dicomstore.scanner import scan_directory
from dicomstore.sender import (
    FileSeries,
    GeneratedSeries,
    SendRequest,
    SendTarget,
    send_job,
)

PORT = 11113  # separate from test_e2e_send.py to avoid collisions


@pytest.fixture()
def receiver(tmp_path):
    cfg = config_mod.Config(port=PORT, storage_path=str(tmp_path / "data"))
    manager = ReceiverManager()
    manager.start(cfg)
    yield manager, cfg, tmp_path / "data"
    manager.stop()


def wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def study_json(folder: Path) -> dict:
    return json.loads((folder / "study.json").read_text(encoding="utf-8"))


def make_target(port=PORT) -> SendTarget:
    return SendTarget(host="localhost", port=port, called_ae="DICOMSTORE")


def make_request(**kwargs) -> SendRequest:
    return SendRequest(target=make_target(kwargs.pop("port", PORT)), **kwargs)


def test_generated_send_single_worker(receiver):
    _, _, data_dir = receiver
    plan = build_plan(["CT", "MR"], 2, 3, patient_name="SEND^ONE", accession="SEND-1")
    work = [GeneratedSeries(plan=plan, spec=s) for s in plan.series]
    progress = SendProgress(plan.instance_count, len(work))
    cancel = threading.Event()
    send_job(work, make_request(), progress, cancel)

    assert progress.snapshot()["sent_ok"] == 6
    assert progress.snapshot()["failed"] == 0
    folder = data_dir / "SEND-1"
    assert wait_for(lambda: (folder / "study.json").exists()
                    and len(list(folder.glob("*.dcm"))) == 6)
    doc = study_json(folder)
    assert doc["InstanceCount"] == 6
    assert doc["Modalities"] == ["CT", "MR"]
    assert doc["PatientName"] == "SEND^ONE"


def test_generated_send_concurrent_workers(receiver):
    _, _, data_dir = receiver
    plan = build_plan(["CT", "MR"], 4, 25, accession="SEND-CONC")
    work = [GeneratedSeries(plan=plan, spec=s) for s in plan.series]
    progress = SendProgress(plan.instance_count, len(work))
    send_job(work, make_request(concurrency=3), progress, threading.Event())

    snap = progress.snapshot()
    assert snap["sent_ok"] == 100
    assert snap["failed"] == 0
    folder = data_dir / "SEND-CONC"
    assert wait_for(lambda: (folder / "study.json").exists()
                    and len(list(folder.glob("*.dcm"))) == 100)
    assert study_json(folder)["InstanceCount"] == 100


def test_delay_throttles(receiver):
    _, _, _ = receiver
    plan = build_plan(["CT"], 1, 4)
    work = [GeneratedSeries(plan=plan, spec=plan.series[0])]
    progress = SendProgress(4, 1)
    started = time.monotonic()
    send_job(work, make_request(delay_ms=100), progress, threading.Event())
    elapsed = time.monotonic() - started
    assert progress.snapshot()["sent_ok"] == 4
    # 4 sends with >=100ms pacing between them: at least ~300ms total.
    assert elapsed >= 0.25


def test_connection_refused_fails_job(tmp_path):
    plan = build_plan(["CT"], 1, 2)
    work = [GeneratedSeries(plan=plan, spec=plan.series[0])]
    progress = SendProgress(2, 1)
    # Port 1 on localhost: nothing listens there.
    send_job(work, make_request(port=1), progress, threading.Event())
    snap = progress.snapshot()
    assert snap["failed"] == 2
    assert snap["sent_ok"] == 0
    assert snap["errors"]


def test_cancel_mid_send(receiver):
    _, _, _ = receiver
    plan = build_plan(["CT"], 1, 500)
    work = [GeneratedSeries(plan=plan, spec=plan.series[0])]
    progress = SendProgress(500, 1)
    cancel = threading.Event()
    thread = threading.Thread(
        target=send_job, args=(work, make_request(delay_ms=20), progress, cancel))
    thread.start()
    # Wait until some progress, then cancel.
    assert wait_for(lambda: progress.snapshot()["sent_ok"] >= 3)
    cancel.set()
    thread.join(timeout=15)
    assert not thread.is_alive()
    snap = progress.snapshot()
    assert snap["sent_ok"] < 500


def test_file_series_send(receiver, tmp_path):
    _, _, data_dir = receiver
    # Write a generated study to disk, then send it as FileSeries.
    plan = build_plan(["CT", "MR"], 2, 3, accession="FILE-SEND")
    src = tmp_path / "src"
    src.mkdir()
    for i, ds in enumerate(iter_instances(plan)):
        ds.save_as(src / f"inst_{i:03d}.dcm", enforce_file_format=True)

    scan = scan_directory(src)
    assert len(scan.studies) == 1
    work = [FileSeries(series_uid=s.series_uid, modality=s.modality, refs=s.instances)
            for s in scan.studies[0].series]
    progress = SendProgress(sum(w.instance_count for w in work), len(work))
    send_job(work, make_request(), progress, threading.Event())

    assert progress.snapshot()["sent_ok"] == 6
    folder = data_dir / "FILE-SEND"
    assert wait_for(lambda: (folder / "study.json").exists()
                    and len(list(folder.glob("*.dcm"))) == 6)
    assert study_json(folder)["Modalities"] == ["CT", "MR"]
