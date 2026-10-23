"""E2E: a real SCU sends synthetic DICOM into the receiver over localhost."""

from __future__ import annotations

import threading

import pytest
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import CTImageStorage, MRImageStorage, ExplicitVRLittleEndian
from pynetdicom import AE

from dicomstore import config as config_mod
from dicomstore.receiver import ReceiverManager

PORT = 11112  # tests bind their own server; no web app involved


def make_instance(sop_instance_uid: str, study_uid: str, accession: str,
                  modality: str = "CT", sop_class=CTImageStorage) -> Dataset:
    ds = Dataset()
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_instance_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = "1.2.826.0.1.3680043.8.498.2"
    ds.AccessionNumber = accession
    ds.PatientName = "TEST^PATIENT"
    ds.PatientID = "PID1"
    ds.Modality = modality
    ds.StudyDate = "20261021"
    ds.StudyDescription = "E2E test study"
    ds.Rows, ds.Columns = 8, 8
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 8, 8, 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = bytes(range(64))

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = sop_instance_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = meta
    return ds


def send_instances(port: int, instances: list[Dataset]) -> list[int]:
    ae = AE(ae_title="TESTSCU")
    for sop_class in {i.SOPClassUID for i in instances}:
        ae.add_requested_context(sop_class, ExplicitVRLittleEndian)
    assoc = ae.associate("localhost", port)
    assert assoc.is_established, "SCU association failed"
    statuses = [assoc.send_c_store(ds) for ds in instances]
    assoc.release()
    return statuses


@pytest.fixture()
def receiver(tmp_path):
    cfg = config_mod.Config(port=PORT, storage_path=str(tmp_path / "data"))
    manager = ReceiverManager()
    manager.start(cfg)
    yield manager, cfg
    manager.stop()


def wait_for(predicate, timeout=10.0):
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_three_instance_study(receiver, tmp_path):
    _, cfg = receiver
    study_uid = "1.2.826.0.1.3680043.8.498.100"
    instances = [make_instance(f"1.2.826.0.1.3680043.8.498.1{i}", study_uid, "ACC-E2E-1")
                 for i in range(3)]
    statuses = send_instances(PORT, instances)
    assert all(int(s.Status) == 0x0000 for s in statuses)

    folder = tmp_path / "data" / "ACC-E2E-1"
    assert wait_for(lambda: (folder / "study.json").exists()
                    and len(list(folder.glob("*.dcm"))) == 3)
    import json
    doc = json.loads((folder / "study.json").read_text(encoding="utf-8"))
    assert doc["InstanceCount"] == 3
    assert doc["AccessionNumber"] == "ACC-E2E-1"
    assert doc["PatientName"] == "TEST^PATIENT"


def test_missing_accession_goes_to_fallback(receiver, tmp_path):
    _, cfg = receiver
    study_uid = "1.2.826.0.1.3680043.8.498.200"
    ds = make_instance("1.2.826.0.1.3680043.8.498.201", study_uid, "")
    statuses = send_instances(PORT, [ds])
    assert int(statuses[0].Status) == 0x0000

    folder = tmp_path / "data" / f"_no_accession_{study_uid}"
    assert wait_for(lambda: (folder / "study.json").exists())


def test_duplicate_overwrites(receiver, tmp_path):
    _, cfg = receiver
    study_uid = "1.2.826.0.1.3680043.8.498.300"
    instances = [make_instance(f"1.2.826.0.1.3680043.8.498.3{i}", study_uid, "ACC-E2E-3")
                 for i in range(3)]
    send_instances(PORT, instances)
    # Re-send the whole study
    statuses = send_instances(PORT, instances)
    assert all(int(s.Status) == 0x0000 for s in statuses)

    folder = tmp_path / "data" / "ACC-E2E-3"
    assert wait_for(lambda: len(list(folder.glob("*.dcm"))) == 3)
    import json
    doc = json.loads((folder / "study.json").read_text(encoding="utf-8"))
    assert doc["InstanceCount"] == 3


def test_mixed_modalities_in_one_study(receiver, tmp_path):
    _, cfg = receiver
    study_uid = "1.2.826.0.1.3680043.8.498.400"
    instances = [
        make_instance("1.2.826.0.1.3680043.8.498.401", study_uid, "ACC-E2E-4",
                      modality="CT", sop_class=CTImageStorage),
        make_instance("1.2.826.0.1.3680043.8.498.402", study_uid, "ACC-E2E-4",
                      modality="MR", sop_class=MRImageStorage),
    ]
    statuses = send_instances(PORT, instances)
    assert all(int(s.Status) == 0x0000 for s in statuses)

    folder = tmp_path / "data" / "ACC-E2E-4"
    assert wait_for(lambda: (folder / "study.json").exists())
    import json
    doc = json.loads((folder / "study.json").read_text(encoding="utf-8"))
    assert doc["Modalities"] == ["CT", "MR"]


def test_concurrent_senders(receiver, tmp_path):
    _, cfg = receiver
    results = {}

    def sender(name: str, base: int):
        study_uid = f"1.2.826.0.1.3680043.8.498.{base}9"
        instances = [make_instance(f"1.2.826.0.1.3680043.8.498.{base}{i}",
                                   study_uid, f"ACC-{name}") for i in range(5)]
        results[name] = send_instances(PORT, instances)

    threads = [threading.Thread(target=sender, args=(n, b)) for n, b in [("A", 500), ("B", 600)]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert all(int(s.Status) == 0x0000 for s in results["A"])
    assert all(int(s.Status) == 0x0000 for s in results["B"])
    import json
    for name in ("A", "B"):
        folder = tmp_path / "data" / f"ACC-{name}"
        assert wait_for(lambda: (folder / "study.json").exists()
                        and len(list(folder.glob("*.dcm"))) == 5)
        doc = json.loads((folder / "study.json").read_text(encoding="utf-8"))
        assert doc["InstanceCount"] == 5
