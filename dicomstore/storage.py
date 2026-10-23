"""Filesystem layout: sanitization, path derivation, atomic writes."""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# Characters invalid on Linux and Windows filesystems, plus control characters.
_INVALID_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f\x7f]')
_MAX_COMPONENT_LEN = 80


def sanitize_component(value: str | None) -> str:
    """Make a DICOM value safe as a single path component.

    Accession numbers are case-sensitive identifiers, so no lowercasing.
    """
    if value is None:
        return "UNKNOWN"
    cleaned = value.strip(" .\t\n\r\f\v")
    cleaned = _INVALID_CHARS.sub("_", cleaned)
    cleaned = cleaned.strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned)
    if not cleaned:
        return "UNKNOWN"
    return cleaned[:_MAX_COMPONENT_LEN]


def study_folder(storage_root: Path, accession_number: str | None,
                 study_uid: str | None) -> Path:
    """Derive the study folder for an instance.

    Accession number names the folder; instances without one fall back to
    ``_no_accession_<StudyInstanceUID>`` so they remain grouped per study.
    """
    if accession_number:
        return storage_root / sanitize_component(accession_number)
    if study_uid:
        return storage_root / sanitize_component("_no_accession_" + study_uid)
    return storage_root / "_no_accession_unknown"


def atomic_write_bytes(directory: Path, filename: str, data: bytes) -> Path:
    """Write data to ``directory/filename`` atomically (temp file + os.replace)."""
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp_", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        target = directory / filename
        os.replace(tmp, target)
        return target
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(directory: Path, filename: str, text: str) -> Path:
    return atomic_write_bytes(directory, filename, text.encode("utf-8"))
