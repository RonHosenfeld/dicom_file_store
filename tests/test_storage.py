import pytest

from dicomstore.storage import atomic_write_bytes, sanitize_component, study_folder


def test_sanitize_replaces_invalid_chars():
    assert sanitize_component("A/B\\C:D*E?F\"G<H>I|J") == "A_B_C_D_E_F_G_H_I_J"


def test_sanitize_control_chars_and_trim():
    assert sanitize_component("  .ABC.\t ") == "ABC"


def test_sanitize_empty_and_none():
    assert sanitize_component("") == "UNKNOWN"
    assert sanitize_component(None) == "UNKNOWN"
    assert sanitize_component("   ") == "UNKNOWN"
    assert sanitize_component("...") == "UNKNOWN"


def test_sanitize_preserves_case():
    assert sanitize_component("abcDEF123") == "abcDEF123"


def test_sanitize_truncates_long_values():
    assert len(sanitize_component("X" * 500)) == 80


def test_study_folder_with_accession(tmp_path):
    folder = study_folder(tmp_path, "ACC-001", "1.2.3")
    assert folder == tmp_path / "ACC-001"


def test_study_folder_fallback_no_accession(tmp_path):
    folder = study_folder(tmp_path, "", "1.2.99.1")
    assert folder == tmp_path / "_no_accession_1.2.99.1"


def test_study_folder_fallback_nothing(tmp_path):
    folder = study_folder(tmp_path, None, None)
    assert folder == tmp_path / "_no_accession_unknown"


def test_atomic_write_bytes(tmp_path):
    target = atomic_write_bytes(tmp_path, "out.bin", b"hello")
    assert target.read_bytes() == b"hello"
    leftovers = [p for p in tmp_path.iterdir() if p.name != "out.bin"]
    assert leftovers == []


def test_atomic_write_overwrites(tmp_path):
    atomic_write_bytes(tmp_path, "out.bin", b"one")
    atomic_write_bytes(tmp_path, "out.bin", b"two")
    assert (tmp_path / "out.bin").read_bytes() == b"two"


@pytest.mark.parametrize("bad", ["a/b", "a\\b", "a:b", "a\x00b"])
def test_study_folder_sanitizes_accession(tmp_path, bad):
    folder = study_folder(tmp_path, bad, "1.2.3")
    assert folder.parent == tmp_path
    assert "/" not in folder.name and "\\" not in folder.name
