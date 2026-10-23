"""Load an ebook file into a Source: either an EPUB (structured) or plain text."""
from __future__ import annotations

import re
import shutil
import subprocess
from functools import cached_property
from pathlib import Path

from ebooklib import epub

from ..model import Cover
from .epub_cover import find_cover, sniff
from .epub_text import whole_text

CALIBRE_EXTS = {".mobi", ".azw", ".azw3", ".fb2", ".fbz", ".docx", ".rtf", ".odt", ".lit", ".htmlz", ".kfx"}
TEXT_EXTS = {".txt", ".md", ".markdown"}
SUPPORTED = {".epub", ".pdf"} | CALIBRE_EXTS | TEXT_EXTS
VI_CHARS = re.compile(r"[ăâđêôơưàáạảãằắặẳẵầấậẩẫèéẹẻẽềếệểễìíịỉĩòóọỏõồốộổỗờớợởỡùúụủũừứựửữỳýỵỷỹ]", re.I)


class UnsupportedFormat(Exception):
    pass


class IngestError(Exception):
    pass


def guess_language(text: str) -> str:
    return "vi" if len(VI_CHARS.findall(text)) / max(1, len(text)) > 0.02 else "en"


class Source:
    def __init__(self, path: Path, title: str, author: str, declared_language: str,
                 cover: Cover | None, epub_book=None, text: str | None = None):
        self.path, self.title, self.author, self.cover = path, title, author, cover
        self.declared_language, self.epub, self._text = declared_language, epub_book, text

    @property
    def kind(self) -> str:
        return "epub" if self.epub is not None else "text"

    @cached_property
    def plain_text(self) -> str:
        return self._text if self._text is not None else whole_text(self.epub)

    @cached_property
    def language(self) -> str:
        detected = guess_language(self.plain_text[:5000])
        declared = self.declared_language[:2].lower()
        return detected if declared in ("", "en") and detected == "vi" else (declared or detected)


COVER_DIR = ".cover"


def chosen_cover(path: Path) -> Cover | None:
    """A cover picked for this book file (for example Calibre-Web's own), kept beside it as .cover/cover.jpg or .png.
    It wins over whatever the file contains, because it is what the person chose to see."""
    folder = path.parent / COVER_DIR
    for ext in ("jpg", "png"):
        try:
            data = (folder / f"cover.{ext}").read_bytes()
        except OSError:
            continue
        if sniff(data) == ext:
            return Cover(data, ext)
    return None


def load_source(path: Path, cache_dir: Path | None = None) -> Source:
    source = _load_source(path, cache_dir)
    chosen = chosen_cover(path)
    if chosen is not None:
        source.cover = chosen
    return source


def _load_source(path: Path, cache_dir: Path | None = None) -> Source:
    ext = path.suffix.lower()
    if ext == ".epub":
        return _epub_source(path, path.stem)
    if ext in TEXT_EXTS:
        text = path.read_text(encoding="utf8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        return Source(path, path.stem, "Unknown", "", None, text=text)
    if ext == ".pdf":
        return Source(path, path.stem, "Unknown", "", _pdf_cover(path), text=_pdf_text(path))
    if ext in CALIBRE_EXTS:
        return _calibre_source(path, cache_dir)
    raise UnsupportedFormat(ext or path.name)


def _meta(book, name: str) -> str:
    values = book.get_metadata("DC", name)
    return (values[0][0] or "").strip() if values else ""


def _epub_source(path: Path, fallback_title: str) -> Source:
    try:
        book = epub.read_epub(str(path))
    except Exception as exc:
        raise IngestError(f"could not open the EPUB: {exc}") from exc
    return Source(path, _meta(book, "title") or fallback_title, _meta(book, "creator") or "Unknown",
                  _meta(book, "language"), find_cover(path), epub_book=book)


def _pdf_text(path: Path) -> str:
    if not shutil.which("pdftotext"):
        raise IngestError("pdftotext is not installed")
    proc = subprocess.run(["pdftotext", "-enc", "UTF-8", str(path), "-"], capture_output=True, check=False)
    if proc.returncode != 0:
        raise IngestError(f"pdftotext failed: {proc.stderr.decode(errors='replace')[:200]}")
    text = proc.stdout.decode("utf8", errors="replace").replace("\f", "\n")
    if len(text.strip()) < 200:
        raise IngestError("PDF has no text layer (scanned PDFs need OCR first)")
    return re.sub(r"(?<=\w)-\n(?=\w)", "", text)


def _pdf_cover(path: Path) -> Cover | None:
    """The first page, as a picture. A PDF has no cover of its own, and its first page usually is one."""
    if not shutil.which("pdftoppm"):
        return None
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        stem = Path(d) / "page"
        proc = subprocess.run(["pdftoppm", "-jpeg", "-r", "90", "-f", "1", "-l", "1", "-singlefile", str(path), str(stem)],
                              capture_output=True, check=False, timeout=60)
        out = stem.with_suffix(".jpg")
        return Cover(out.read_bytes(), "jpg") if proc.returncode == 0 and out.exists() else None


def _calibre_source(path: Path, cache_dir: Path | None) -> Source:
    if not shutil.which("ebook-convert"):
        raise IngestError(f"{path.suffix} needs Calibre (ebook-convert), which is not installed in this image")
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        out = (cache_dir / f"{path.stem}.converted.epub") if cache_dir else Path(d) / "book.epub"
        if not out.exists():
            proc = subprocess.run(["ebook-convert", str(path), str(out)], capture_output=True, check=False)
            if proc.returncode != 0 or not out.exists():
                out.unlink(missing_ok=True)
                raise IngestError(f"Calibre could not convert {path.name}: {proc.stderr.decode(errors='replace')[-300:]}")
        return _epub_source(out, path.stem)
