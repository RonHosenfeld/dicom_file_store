"""Web-level tests for the Send tab using FastAPI TestClient."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dicomstore import config as config_mod
from dicomstore.generator import build_plan, iter_instances
from dicomstore.receiver import ReceiverManager
from dicomstore.webapp import create_app

PORT = 11114  # dedicated port for this module


@pytest.fixture()
def receiver(tmp_path):
    cfg = config_mod.Config(port=PORT, storage_path=str(tmp_path / "data"))
    manager = ReceiverManager()
    manager.start(cfg)
    yield manager, tmp_path / "data"
    manager.stop()


@pytest.fixture()
def client(receiver, tmp_path, monkeypatch):
    cfg = config_mod.Config(port=PORT, storage_path=str(tmp_path / "data"))
    monkeypatch.setattr(config_mod, "load", lambda: cfg)
    app = create_app()
    with TestClient(app) as client:
        yield client, tmp_path / "data"


def wait_for(predicate, timeout=15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_send_page_renders(client):
    client_obj, _ = client
    resp = client_obj.get("/send")
    assert resp.status_code == 200
    assert "Generate" in resp.text
    assert "Send from directory" in resp.text
    assert "Jobs" in resp.text


def test_nav_has_send_link(client):
    client_obj, _ = client
    resp = client_obj.get("/")
    assert resp.status_code == 200
    assert '/send"' in resp.text


def test_generate_send_end_to_end(client):
    client_obj, data_dir = client
    resp = client_obj.post("/send/generate", follow_redirects=False, data={
        "host": "localhost", "port": PORT, "called_ae": "DICOMSTORE",
        "calling_ae": "WEBTEST-SCU", "concurrency": "1", "delay_ms": "0",
        "max_per_second": "0", "modalities": ["CT", "MR"],
        "series_count": "2", "images_per_series": "3",
        "patient_name": "WEB^TEST", "patient_id": "PID-WEB",
        "accession": "WEB-ACC-1", "study_description": "web test",
        "study_date": "",
    })
    assert resp.status_code == 303

    # The job should appear on /send and complete.
    assert wait_for(lambda: any(
        j.state == "completed" for j in client_obj.app.state.jobs.all()))

    folder = data_dir / "WEB-ACC-1"
    assert wait_for(lambda: (folder / "study.json").exists())
    import json
    doc = json.loads((folder / "study.json").read_text(encoding="utf-8"))
    assert doc["InstanceCount"] == 6
    assert doc["Modalities"] == ["CT", "MR"]
    assert doc["PatientName"] == "WEB^TEST"


def test_progress_endpoint(client):
    client_obj, _ = client
    resp = client_obj.post("/send/generate", follow_redirects=False, data={
        "host": "localhost", "port": PORT, "called_ae": "DICOMSTORE",
        "calling_ae": "WEBTEST-SCU", "concurrency": "1", "delay_ms": "0",
        "max_per_second": "0", "modalities": ["CT"], "series_count": "1",
        "images_per_series": "1", "accession": "WEB-ACC-2",
    })
    assert resp.status_code == 303
    assert wait_for(lambda: any(
        j.state == "completed" for j in client_obj.app.state.jobs.all()))
    job = client_obj.app.state.jobs.all()[0]
    resp = client_obj.get(f"/send/jobs/{job.id}/progress")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "completed"
    assert data["progress"]["sent_ok"] == 1
    assert data["progress"]["total_instances"] == 1


def test_generate_requires_modality(client):
    client_obj, _ = client
    resp = client_obj.post("/send/generate", follow_redirects=False, data={
        "host": "localhost", "port": PORT, "called_ae": "DICOMSTORE",
        "modalities": [], "series_count": "1", "images_per_series": "1",
    })
    assert resp.status_code == 400


def test_bad_port_rejected(client):
    client_obj, _ = client
    resp = client_obj.post("/send/generate", follow_redirects=False, data={
        "host": "localhost", "port": "99999", "called_ae": "DICOMSTORE",
        "modalities": ["CT"], "series_count": "1", "images_per_series": "1",
    })
    assert resp.status_code == 400


def test_scan_and_send_directory(client, receiver, tmp_path):
    client_obj, data_dir = client
    # Build a source directory with one study.
    src = tmp_path / "realdata"
    src.mkdir()
    plan = build_plan(["CT"], 2, 2, accession="DIR-SEND")
    for i, ds in enumerate(iter_instances(plan)):
        ds.save_as(src / f"inst_{i}.dcm", enforce_file_format=True)

    resp = client_obj.post("/send/scan", follow_redirects=False, data={"directory": str(src)})
    assert resp.status_code == 303
    assert "scan=" in resp.headers["location"]

    # Pull the scan id out of app.state (avoids parsing the redirect page).
    scan_id = next(iter(client_obj.app.state.scans))
    resp = client_obj.post("/send/directory", follow_redirects=False, data={
        "scan_id": scan_id, "host": "localhost", "port": PORT,
        "called_ae": "DICOMSTORE", "concurrency": "1", "delay_ms": "0",
        "max_per_second": "0",
        "study_uid": [plan.study_uid],
    })
    assert resp.status_code == 303

    assert wait_for(lambda: any(
        j.state == "completed" for j in client_obj.app.state.jobs.all()))
    folder = data_dir / "DIR-SEND"
    assert wait_for(lambda: (folder / "study.json").exists()
                    and len(list(folder.glob("*.dcm"))) == 4)


def test_bad_directory_rejected(client):
    client_obj, _ = client
    resp = client_obj.post("/send/scan", follow_redirects=False, data={"directory": "/nonexistent/path"})
    assert resp.status_code == 400


def test_unknown_job_404(client):
    client_obj, _ = client
    resp = client_obj.get("/send/jobs/nope/progress")
    assert resp.status_code == 404
