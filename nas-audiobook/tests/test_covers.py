"""The library should show the book's own cover, found the way e-readers find it."""
import os
import stat
import zipfile
from pathlib import Path

import pytest

from app.books.epub_cover import find_cover, sniff
from app.books.source import load_source
from app.library import Library
from app.model import Cover

JPEG = b"\xff\xd8\xff\xe0" + b"jpeg-bytes"
PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes"
OTHER = b"\xff\xd8\xff\xe0" + b"some-other-picture"

CONTAINER = """<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"""


def build(path, *, manifest, metadata="", guide="", files=None):
    items = "".join(f'<item id="{i}" href="{h}" media-type="{t}"{" properties=%r" % p if p else ""}/>'.replace("'", '"')
                    for i, h, t, p in manifest)
    opf = (f'<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
           f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>{metadata}</metadata>'
           f'<manifest>{items}</manifest><spine/>{"<guide>" + guide + "</guide>" if guide else ""}</package>')
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        for name, data in (files or {}).items():
            z.writestr(f"OEBPS/{name}", data)
    return path


def test_the_cover_the_book_declares_in_its_metadata_wins_over_a_decoy_named_cover(tmp_path):
    book = build(tmp_path / "a.epub", metadata='<meta name="cover" content="img1"/>',
                 manifest=[("img1", "images/img001.png", "image/png", ""), ("old", "images/cover-old.jpg", "image/jpeg", "")],
                 files={"images/img001.png": JPEG, "images/cover-old.jpg": OTHER})
    assert find_cover(book) == Cover(JPEG, "jpg")  # the bytes decide the type, not the .png name


def test_epub3_cover_image_property(tmp_path):
    book = build(tmp_path / "b.epub", manifest=[("a", "a.jpg", "image/jpeg", ""), ("c", "pics/front.png", "image/png", "cover-image")],
                 files={"a.jpg": OTHER, "pics/front.png": PNG})
    assert find_cover(book) == Cover(PNG, "png")


def test_a_calibre_style_cover_page_points_at_the_picture_through_svg(tmp_path):
    page = '<html><body><svg xmlns:xlink="http://www.w3.org/1999/xlink"><image width="600" xlink:href="../img/pic%20one.jpg"/></svg></body></html>'
    book = build(tmp_path / "c.epub", guide='<reference type="cover" href="text/titlepage.xhtml#top" title="Cover"/>',
                 manifest=[("p", "text/titlepage.xhtml", "application/xhtml+xml", ""), ("i", "img/pic one.jpg", "image/jpeg", "")],
                 files={"text/titlepage.xhtml": page, "img/pic one.jpg": JPEG})
    assert find_cover(book) == Cover(JPEG, "jpg")


def test_a_cover_page_with_a_plain_img_tag(tmp_path):
    book = build(tmp_path / "d.epub", guide='<reference type="cover" href="cover.html"/>',
                 manifest=[("p", "cover.html", "application/xhtml+xml", ""), ("i", "front.jpg", "image/jpeg", "")],
                 files={"cover.html": '<html><body><img alt="" src="front.jpg"/></body></html>', "front.jpg": JPEG})
    assert find_cover(book) == Cover(JPEG, "jpg")


def test_an_undeclared_cover_is_found_by_its_name_as_a_last_resort(tmp_path):
    book = build(tmp_path / "e.epub", manifest=[("x", "images/inside.jpg", "image/jpeg", ""), ("y", "images/Cover_front.jpg", "image/jpeg", "")],
                 files={"images/inside.jpg": OTHER, "images/Cover_front.jpg": JPEG})
    assert find_cover(book) == Cover(JPEG, "jpg")


def test_a_book_with_pictures_but_no_cover_gets_none_rather_than_a_random_picture(tmp_path):
    book = build(tmp_path / "f.epub", manifest=[("x", "images/inside.jpg", "image/jpeg", "")], files={"images/inside.jpg": OTHER})
    assert find_cover(book) is None


def test_things_that_are_not_images_or_not_epubs_are_skipped(tmp_path):
    book = build(tmp_path / "g.epub", metadata='<meta name="cover" content="c"/>',
                 manifest=[("c", "cover.jpg", "image/jpeg", ""), ("d", "cover2.jpg", "image/jpeg", "")],
                 files={"cover.jpg": b"<html>not a picture</html>", "cover2.jpg": JPEG})
    assert find_cover(book) == Cover(JPEG, "jpg")
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a zip")
    assert find_cover(broken) is None
    assert sniff(b"GIF89a") is None and sniff(b"") is None


SAMPLE = next(iter((Path(__file__).parents[2] / "books").glob("*.epub")), None)


@pytest.mark.skipif(SAMPLE is None, reason="no sample book in ../books")
def test_the_sample_book_gets_the_image_its_metadata_names(tmp_path):
    with zipfile.ZipFile(SAMPLE) as z:
        expected = z.read("cover.jpeg")
    assert find_cover(SAMPLE) == Cover(expected, "jpg")
    assert load_source(SAMPLE).cover == Cover(expected, "jpg")


def test_a_better_cover_replaces_the_one_saved_earlier(tmp_path):
    library = Library(tmp_path / "lib")
    md5 = "a" * 32
    kw = dict(title="T", author="A", language="vi", source_name="b.epub")
    library.ensure_book(md5, cover=Cover(OTHER, "jpg"), **kw)
    library.ensure_book(md5, cover=Cover(PNG, "png"), **kw)
    folder = library.dir(md5)
    assert (folder / "cover.png").read_bytes() == PNG and not (folder / "cover.jpg").exists()
    assert library.manifest(md5)["cover"] == "cover.png"
    library.ensure_book(md5, cover=None, **kw)  # a re-run that finds no cover keeps the one it has
    assert library.manifest(md5)["cover"] == "cover.png" and (folder / "cover.png").exists()


def test_a_pdfs_first_page_becomes_its_cover(tmp_path, monkeypatch):
    fake = tmp_path / "bin"
    fake.mkdir()
    tool = fake / "pdftoppm"
    tool.write_text('#!/bin/sh\nfor last; do :; done\nprintf "\\377\\330\\377\\340page-one" > "$last.jpg"\n')
    tool.chmod(tool.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    from app.books.source import _pdf_cover

    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    cover = _pdf_cover(pdf)
    assert cover == Cover(b"\xff\xd8\xff\xe0page-one", "jpg")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert _pdf_cover(pdf) is None
