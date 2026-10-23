import shutil

import pytest

from app.ingest import IngestError, load_book
from app.text import TextOptions

pytestmark = pytest.mark.skipif(not shutil.which("pdftotext"), reason="poppler is not installed")

LINES = ["Chapter 1", "The quick brown fox jumps over the lazy dog near the river bank today.",
         "A second sentence follows so that the page has enough text to be read aloud.",
         "A third sentence keeps the chapter above the minimum length that the splitter requires."] * 2


def make_pdf(path, lines):
    stream = "BT /F1 12 Tf 14 TL 40 760 Td " + " ".join(f"({ln}) Tj T*" for ln in lines) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = "%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n" + "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))


def test_pdf_with_text_layer_becomes_spoken_chapters(tmp_path):
    pdf = tmp_path / "doc.pdf"
    make_pdf(pdf, LINES)
    book = load_book(pdf, TextOptions(False))
    spoken = " ".join(c.text for ch in book.chapters for c in ch.clips)
    assert "quick brown fox" in spoken and book.language == "en"


def test_pdf_without_text_is_rejected_with_a_clear_message(tmp_path):
    pdf = tmp_path / "empty.pdf"
    make_pdf(pdf, [""])
    with pytest.raises(IngestError, match="no text layer"):
        load_book(pdf, TextOptions(False))
