"""Synthetic exam generation: realistic metadata + gradient pixel data.

Datasets are produced lazily by :func:`iter_instances` so large load-test
plans (thousands of instances) never materialize in memory at once. Pixel
data is built once per series and reused across its instances — instance
identity comes from the UIDs, and identical pixels within a series are both
realistic and cheap.
"""

from __future__ import annotations

import random
import uuid
from array import array
from dataclasses import dataclass, field
from datetime import date
from itertools import cycle

from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ComputedRadiographyImageStorage,
    DigitalXRayImageStorageForPresentation,
    ExplicitVRLittleEndian,
    MRImageStorage,
    NuclearMedicineImageStorage,
    PositronEmissionTomographyImageStorage,
    SecondaryCaptureImageStorage,
    UltrasoundImageStorage,
    generate_uid,
)

UID_PREFIX = "1.2.826.0.1.3680043.8.498."

# Largest per-instance pixel payload the form will accept.
MAX_PIXEL_BYTES = 4096 * 4096 * 2


@dataclass(frozen=True)
class ModalitySpec:
    key: str
    sop_class_uid: str
    rows: int
    cols: int
    bits: int


# Verified against the installed pydicom 3.x pydicom.uid names.
MODALITY_SPECS: dict[str, ModalitySpec] = {
    "CT": ModalitySpec("CT", CTImageStorage, 512, 512, 16),
    "MR": ModalitySpec("MR", MRImageStorage, 256, 256, 16),
    "CR": ModalitySpec("CR", ComputedRadiographyImageStorage, 1024, 1024, 16),
    "DX": ModalitySpec("DX", DigitalXRayImageStorageForPresentation, 1024, 1024, 16),
    "US": ModalitySpec("US", UltrasoundImageStorage, 640, 480, 8),
    "PT": ModalitySpec("PT", PositronEmissionTomographyImageStorage, 128, 128, 16),
    "NM": ModalitySpec("NM", NuclearMedicineImageStorage, 256, 256, 16),
    "SC": ModalitySpec("SC", SecondaryCaptureImageStorage, 640, 480, 8),
}


@dataclass
class SeriesSpec:
    modality: str
    series_uid: str
    series_number: int
    series_description: str
    rows: int
    cols: int
    bits: int
    sop_class_uid: str
    image_count: int


@dataclass
class ExamPlan:
    patient_name: str
    patient_id: str
    accession: str
    study_uid: str
    study_description: str
    study_date: str
    study_time: str
    series: list[SeriesSpec] = field(default_factory=list)

    @property
    def instance_count(self) -> int:
        return sum(s.image_count for s in self.series)


def build_plan(modalities: list[str], series_count: int, images_per_series: int,
               patient_name: str = "", patient_id: str = "", accession: str = "",
               study_description: str = "", study_date: str = "") -> ExamPlan:
    """Build a study plan cycling through ``modalities`` across its series."""
    if not modalities:
        raise ValueError("At least one modality is required")
    unknown = [m for m in modalities if m not in MODALITY_SPECS]
    if unknown:
        raise ValueError(f"Unknown modality: {', '.join(unknown)}")
    if series_count < 1:
        raise ValueError("series_count must be >= 1")
    if images_per_series < 1:
        raise ValueError("images_per_series must be >= 1")

    patient_name = patient_name.strip() or f"TEST^PATIENT{random.randint(1, 999):03d}"
    patient_id = patient_id.strip() or f"PID{random.randint(100000, 999999)}"
    accession = accession.strip() or f"SYN-{uuid.uuid4().hex[:6].upper()}"
    study_date = study_date.strip() or date.today().strftime("%Y%m%d")

    # One shared pixel-seed per series would make every series identical;
    # seed from the series UID so each series gets its own deterministic look.
    series = []
    for number, modality in zip(range(1, series_count + 1), cycle(modalities)):
        spec = MODALITY_SPECS[modality]
        if spec.rows * spec.cols * (spec.bits // 8) > MAX_PIXEL_BYTES:
            raise ValueError(f"Modality {modality} exceeds the pixel-size cap")
        series.append(SeriesSpec(
            modality=modality,
            series_uid=generate_uid(prefix=UID_PREFIX),
            series_number=number,
            series_description=f"{modality} test series {number}",
            rows=spec.rows,
            cols=spec.cols,
            bits=spec.bits,
            sop_class_uid=spec.sop_class_uid,
            image_count=images_per_series,
        ))

    return ExamPlan(
        patient_name=patient_name,
        patient_id=patient_id,
        accession=accession,
        study_uid=generate_uid(prefix=UID_PREFIX),
        study_description=study_description.strip() or "Synthetic test study",
        study_date=study_date,
        study_time=date.today().strftime("%H%M%S"),
        series=series,
    )


def _pixel_buffer(rows: int, cols: int, bits: int, seed: int) -> bytes:
    """Deterministic gradient + noise frame for one series."""
    rng = random.Random(seed)
    if bits == 8:
        buf = bytearray(rows * cols)
        for r in range(rows):
            base = r * cols
            row_val = (r * 255) // max(rows - 1, 1)
            for c in range(cols):
                buf[base + c] = min(row_val + rng.randrange(16), 255)
        return bytes(buf)
    buf = array("H", bytes(2 * rows * cols))
    for r in range(rows):
        base = r * cols
        row_val = (r * 65535) // max(rows - 1, 1)
        for c in range(cols):
            buf[base + c] = min(row_val + rng.randrange(4096), 65535)
    return buf.tobytes()


def iter_series_instances(plan: ExamPlan, series: SeriesSpec):
    """Lazily yield one Dataset per instance of a single series."""
    pixels = _pixel_buffer(series.rows, series.cols, series.bits,
                           seed=hash(series.series_uid) & 0xFFFFFFFF)
    for instance_number in range(1, series.image_count + 1):
        yield _build_instance(plan, series, pixels, instance_number)


def iter_instances(plan: ExamPlan):
    """Lazily yield one Dataset per instance in the plan."""
    for series in plan.series:
        yield from iter_series_instances(plan, series)


def _build_instance(plan: ExamPlan, series: SeriesSpec, pixels: bytes,
                    instance_number: int) -> Dataset:
    sop_instance_uid = generate_uid(prefix=UID_PREFIX)
    ds = Dataset()
    ds.SOPClassUID = series.sop_class_uid
    ds.SOPInstanceUID = sop_instance_uid
    ds.StudyInstanceUID = plan.study_uid
    ds.SeriesInstanceUID = series.series_uid
    ds.Modality = series.modality
    ds.AccessionNumber = plan.accession
    ds.PatientName = plan.patient_name
    ds.PatientID = plan.patient_id
    ds.StudyDate = plan.study_date
    ds.StudyTime = plan.study_time
    ds.StudyDescription = plan.study_description
    ds.SeriesDescription = series.series_description
    ds.SeriesNumber = series.series_number
    ds.InstanceNumber = instance_number
    ds.Rows = series.rows
    ds.Columns = series.cols
    ds.BitsAllocated = series.bits
    ds.BitsStored = series.bits
    ds.HighBit = series.bits - 1
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = pixels

    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = series.sop_class_uid
    meta.MediaStorageSOPInstanceUID = sop_instance_uid
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta = meta
    return ds
