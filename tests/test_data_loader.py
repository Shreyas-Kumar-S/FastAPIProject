from pathlib import Path

import pytest

from fastapiproject.data_loader import resolve_safe_pdf_path


@pytest.fixture
def sandbox(tmp_path: Path) -> Path:
    (tmp_path / "uploads").mkdir()
    (tmp_path / "uploads" / "doc.pdf").write_bytes(b"%PDF-1.3 fake content")
    (tmp_path / "outside.pdf").write_bytes(b"%PDF-1.3 fake content")
    return tmp_path / "uploads"


def test_relative_path_inside_root_resolves(sandbox: Path):
    resolved = resolve_safe_pdf_path("doc.pdf", base_dir=sandbox)
    assert resolved == (sandbox / "doc.pdf").resolve()


def test_dotdot_traversal_outside_root_is_rejected(sandbox: Path):
    with pytest.raises(ValueError):
        resolve_safe_pdf_path("../outside.pdf", base_dir=sandbox)


def test_absolute_path_outside_root_is_rejected(sandbox: Path):
    outside = sandbox.parent / "outside.pdf"
    with pytest.raises(ValueError):
        resolve_safe_pdf_path(str(outside), base_dir=sandbox)


def test_missing_file_inside_root_raises_file_not_found(sandbox: Path):
    with pytest.raises(FileNotFoundError):
        resolve_safe_pdf_path("missing.pdf", base_dir=sandbox)
