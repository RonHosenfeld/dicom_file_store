"""Scan a local directory for DICOM files and group them into studies.

Reads only the headers (``stop_before_pixels=True``) and keeps per-instance
references to file paths, so scanning a large tree stays cheap and sending
streams files one at a time.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import pydicom
from pydicom.errors import InvalidDicomError

logger = logging.getLogger(__name__)

DEFAULT_MAX_FILES = 20000


@dataclass
class InstanceRef:
    path: str
    sop_instance_uid: str
    sop_class_uid: str
    transfer_syntax_uid: str


@dataclass
class SeriesSummary:
    series_uid: str
    modality: str
    instances: list[InstanceRef] = field(default_factory=list)

    @property
    def instance_count(self) -> int:
        return len(self.instances)


@dataclass
class StudySummary:
    study_uid: str
    patient_name: str = ""
    patient_id: str = ""
    accession: str = ""
    study_date: str = ""
    series: list[SeriesSummary] = field(default_factory=list)

    @property
    def modalities(self) -> list[str]:
        return sorted({s.modality for s in self.series if s.modality})

    @property
    def instance_count(self) -> int:
        return sum(s.instance_count for s in self.series)


@dataclass
class ScanResult:
    root: str
    studies: list[StudySummary] = field(default_factory=list)
    skipped: int = 0
    truncated: bool = False


def _str(ds, keyword: str) -> str:
    value = getattr(ds, keyword, None)
    return str(value) if value is not None else ""


def scan_directory(root: Path, max_files: int = DEFAULT_MAX_FILES) -> ScanResult:
    """Walk ``root`` recursively, grouping DICOM files by study and series."""
    result = ScanResult(root=str(root))
    studies: dict[str, StudySummary] = {}
    series_map: dict[tuple[str, str], SeriesSummary] = {}

    count = 0
    for path in sorted(root.rglob("*")):
        if count >= max_files:
            result.truncated = True
            break
        if not path.is_file():
            continue
        count += 1
        try:
            ds = pydicom.dcmread(path, stop_before_pixels=True)
        except (InvalidDicomError, OSError, ValueError):
            result.skipped += 1
            continue
        study_uid = _str(ds, "StudyInstanceUID")
        series_uid = _str(ds, "SeriesInstanceUID")
        sop_uid = _str(ds, "SOPInstanceUID")
        if not (study_uid and series_uid and sop_uid):
            result.skipped += 1
            continue

        transfer_syntax = ""
        if ds.file_meta and "TransferSyntaxUID" in ds.file_meta:
            transfer_syntax = str(ds.file_meta.TransferSyntaxUID)

        study = studies.get(study_uid)
        if study is None:
            study = StudySummary(
                study_uid=study_uid,
                patient_name=_str(ds, "PatientName"),
                patient_id=_str(ds, "PatientID"),
                accession=_str(ds, "AccessionNumber"),
                study_date=_str(ds, "StudyDate"),
            )
            studies[study_uid] = study
        key = (study_uid, series_uid)
        series = series_map.get(key)
        if series is None:
            series = SeriesSummary(series_uid=series_uid, modality=_str(ds, "Modality"))
            series_map[key] = series
            study.series.append(series)
        series.instances.append(InstanceRef(
            path=str(path),
            sop_instance_uid=sop_uid,
            sop_class_uid=_str(ds, "SOPClassUID"),
            transfer_syntax_uid=transfer_syntax,
        ))

    result.studies = sorted(studies.values(),
                            key=lambda s: s.study_date or "", reverse=True)
    return result
