"""Per-study study.json metadata: read, merge, rewrite atomically.

A single global lock serializes read-merge-write cycles. Instance arrival
rate is low, and this keeps concurrent associations delivering to the same
study consistent without per-study lock bookkeeping.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path

_LOCK = threading.Lock()

STUDY_JSON_NAME = "study.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get_str(ds, keyword: str) -> str:
    value = getattr(ds, keyword, None)
    if value is None:
        return ""
    return str(value)


def update_from_instance(folder: Path, ds, filename: str) -> dict:
    """Merge one received instance into the study's study.json and rewrite it.

    A re-received instance (same SOPInstanceUID) replaces its entry, so
    InstanceCount stays honest under duplicate sends.
    """
    with _LOCK:
        try:
            with open(folder / STUDY_JSON_NAME, encoding="utf-8") as f:
                doc = json.load(f)
            if not isinstance(doc, dict):
                doc = {}
        except (OSError, ValueError):
            doc = {}

        instances = doc.get("Instances", [])
        if not isinstance(instances, list):
            instances = []
        sop_uid = _get_str(ds, "SOPInstanceUID")
        entry = {
            "SOPInstanceUID": sop_uid,
            "SOPClassUID": _get_str(ds, "SOPClassUID"),
            "Modality": _get_str(ds, "Modality"),
            "File": filename,
            "ReceivedAt": _now_iso(),
        }
        instances = [i for i in instances
                     if not (isinstance(i, dict) and i.get("SOPInstanceUID") == sop_uid)]
        instances.append(entry)

        doc.update({
            "PatientName": _get_str(ds, "PatientName"),
            "PatientID": _get_str(ds, "PatientID"),
            "AccessionNumber": _get_str(ds, "AccessionNumber"),
            "StudyInstanceUID": _get_str(ds, "StudyInstanceUID"),
            "StudyDate": _get_str(ds, "StudyDate"),
            "StudyTime": _get_str(ds, "StudyTime"),
            "StudyDescription": _get_str(ds, "StudyDescription"),
            "Modalities": sorted({i["Modality"] for i in instances if i["Modality"]}),
            "InstanceCount": len(instances),
            "Instances": instances,
            "LastUpdated": _now_iso(),
        })

        text = json.dumps(doc, indent=2, ensure_ascii=False)
        from .storage import atomic_write_text
        atomic_write_text(folder, STUDY_JSON_NAME, text + "\n")
        return doc
