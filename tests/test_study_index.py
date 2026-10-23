import json

import pytest
from pydicom.dataset import Dataset

from dicomstore.study_index import update_from_instance


def make_ds(**kwargs) -> Dataset:
    ds = Dataset()
    ds.SOPInstanceUID = kwargs.get("SOPInstanceUID", "1.2.3.1")
    ds.SOPClassUID = kwargs.get("SOPClassUID", "1.2.840.10008.5.1.4.1.1.2")
    ds.AccessionNumber = kwargs.get("AccessionNumber", "ACC1")
    ds.StudyInstanceUID = kwargs.get("StudyInstanceUID", "1.2.3.9")
    ds.PatientName = kwargs.get("PatientName", "DOE^JOHN")
    ds.PatientID = kwargs.get("PatientID", "P1")
    ds.Modality = kwargs.get("Modality", "CT")
    ds.StudyDate = kwargs.get("StudyDate", "20261021")
    return ds


def test_new_instance_creates_json(tmp_path):
    doc = update_from_instance(tmp_path, make_ds(), "1.2.3.1.dcm")
    assert doc["InstanceCount"] == 1
    assert doc["Modalities"] == ["CT"]
    assert doc["Instances"][0]["File"] == "1.2.3.1.dcm"
    saved = json.loads((tmp_path / "study.json").read_text(encoding="utf-8"))
    assert saved == doc


def test_second_instance_merges(tmp_path):
    update_from_instance(tmp_path, make_ds(SOPInstanceUID="1.2.3.1"), "a.dcm")
    doc = update_from_instance(tmp_path, make_ds(SOPInstanceUID="1.2.3.2", Modality="MR"), "b.dcm")
    assert doc["InstanceCount"] == 2
    assert doc["Modalities"] == ["CT", "MR"]


def test_duplicate_replaces_entry(tmp_path):
    update_from_instance(tmp_path, make_ds(SOPInstanceUID="1.2.3.1"), "a.dcm")
    update_from_instance(tmp_path, make_ds(SOPInstanceUID="1.2.3.2"), "b.dcm")
    doc = update_from_instance(tmp_path, make_ds(SOPInstanceUID="1.2.3.1"), "a.dcm")
    assert doc["InstanceCount"] == 2
    assert [i["SOPInstanceUID"] for i in doc["Instances"]] == ["1.2.3.2", "1.2.3.1"]


def test_corrupt_json_tolerated(tmp_path):
    (tmp_path / "study.json").write_text("{not json", encoding="utf-8")
    doc = update_from_instance(tmp_path, make_ds(), "a.dcm")
    assert doc["InstanceCount"] == 1


def test_missing_tags_become_empty_strings(tmp_path):
    ds = Dataset()
    ds.SOPInstanceUID = "1.2.3.1"
    doc = update_from_instance(tmp_path, ds, "a.dcm")
    assert doc["AccessionNumber"] == ""
    assert doc["Modalities"] == []
    assert doc["InstanceCount"] == 1
