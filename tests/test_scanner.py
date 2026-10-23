"""Unit tests for directory scanning and study/series grouping."""

from __future__ import annotations

from pathlib import Path

import pytest

from dicomstore.generator import build_plan, iter_instances
from dicomstore.scanner import scan_directory


@pytest.fixture()
def study_tree(tmp_path: Path) -> Path:
    """Two studies (one CT-only, one CT+MR) written as real DICOM files."""
    for acc in ("SCAN-A", "SCAN-B"):
        modalities = ["CT"] if acc == "SCAN-A" else ["CT", "MR"]
        plan = build_plan(modalities, len(modalities), 2, accession=acc,
                          study_date="20250101" if acc == "SCAN-A" else "20250202")
        subdir = tmp_path / acc
        subdir.mkdir()
        for i, ds in enumerate(iter_instances(plan)):
            ds.save_as(subdir / f"inst_{i:03d}.dcm", enforce_file_format=True)
    (tmp_path / "notes.txt").write_text("not dicom")
    (tmp_path / "empty.dcm").write_bytes(b"")
    (tmp_path / "truncated.dcm").write_bytes(b"\x00" * 100)
    return tmp_path


def test_groups_studies_and_series(study_tree):
    result = scan_directory(study_tree)
    assert len(result.studies) == 2
    assert not result.truncated
    assert result.skipped == 3  # notes.txt, empty.dcm, truncated.dcm

    by_acc = {s.accession: s for s in result.studies}
    a = by_acc["SCAN-A"]
    assert a.instance_count == 2
    assert len(a.series) == 1
    assert a.modalities == ["CT"]

    b = by_acc["SCAN-B"]
    assert b.instance_count == 4
    assert len(b.series) == 2
    assert b.modalities == ["CT", "MR"]


def test_series_instance_refs(study_tree):
    result = scan_directory(study_tree)
    study = next(s for s in result.studies if s.accession == "SCAN-A")
    refs = study.series[0].instances
    assert {r.sop_class_uid for r in refs} == {"1.2.840.10008.5.1.4.1.1.2"}
    assert all(r.transfer_syntax_uid == "1.2.840.10008.1.2.1" for r in refs)
    assert all(Path(r.path).is_file() for r in refs)


def test_sorted_by_study_date_desc(study_tree):
    result = scan_directory(study_tree)
    assert [s.accession for s in result.studies] == ["SCAN-B", "SCAN-A"]


def test_truncation(study_tree):
    result = scan_directory(study_tree, max_files=3)
    assert result.truncated


def test_missing_directory(tmp_path):
    result = scan_directory(tmp_path / "does-not-exist")
    assert result.studies == []
    assert result.skipped == 0


def test_missing_uids_skipped(tmp_path):
    import pydicom
    ds = pydicom.dataset.Dataset()
    ds.PatientName = "ORPHAN"
    ds.save_as(tmp_path / "no_uids.dcm", enforce_file_format=False,
               implicit_vr=True, little_endian=True)
    result = scan_directory(tmp_path)
    assert result.skipped == 1
    assert result.studies == []
