"""Calibre-Web over OPDS, against a fake that answers the way Calibre-Web does (feeds, covers, downloads, Basic auth)."""
import base64
import hashlib
import os
import stat
from xml.sax.saxutils import escape

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.calibre import CalibreClient, CalibreConfig, CalibreError, CalibreStore
from app.jobs import JobStore
from test_api import FakePool

USER, PASSWORD = "reader", "s3cret-pw"
BASE = "http://calibre.test:8083"
FEED = ('<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/"><id>urn:uuid:x</id><title>{title}</title>{head}{body}</feed>')
SEARCH_LINK = '<link rel="search" href="/opds/search/{searchTerms}" type="application/atom+xml"/>'
LONG_SUMMARY = "Một cuốn sách rất dài. " * 40


def nav(title, href, rel="subsection"):
    return (f'<entry><title>{title}</title><id>urn:{title}</id><content type="text">{title} list</content>'
            f'<link href="{href}" type="application/atom+xml;profile=opds-catalog" rel="{rel}"/></entry>')


def book(i, title, authors=("Baird T. Spalding",), formats=("epub", "pdf"), summary="Tóm tắt ngắn."):
    types = {"epub": "application/epub+zip", "pdf": "application/pdf", "cbz": "application/x-cbz", "mobi": "application/x-mobipocket-ebook"}
    links = "".join(f'<link rel="http://opds-spec.org/acquisition" href="/opds/download/{i}/{f}/" length="1234" '
                    f'title="{f.upper()}" type="{types[f]}"/>' for f in formats)
    who = "".join(f"<author><name>{a}</name></author>" for a in authors)
    return (f'<entry><title>{title}</title><id>urn:uuid:{i}</id>{who}<content type="xhtml">'
            f'<div xmlns="http://www.w3.org/1999/xhtml"><p>{summary}</p></div></content>{links}'
            f'<link rel="http://opds-spec.org/image" href="/opds/cover/{i}" type="image/jpeg"/>'
            f'<link rel="http://opds-spec.org/image/thumbnail" href="/opds/thumb/{i}" type="image/jpeg"/></entry>')


class FakeCalibre:
    """Handler for httpx.MockTransport. `books` maps (id, format) to file bytes."""

    def __init__(self, epub_bytes=b"", opds=True, search_link=True):
        self.requests, self.epub, self.opds, self.search_link = [], epub_bytes, opds, search_link

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "calibre.test", f"left the configured server: {request.url}"
        want = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if request.headers.get("authorization") != want:
            return httpx.Response(401, text="Unauthorized Access", headers={"WWW-Authenticate": 'Basic realm="x"'})
        path, query = request.url.path, dict(request.url.params)
        if not self.opds and path.startswith("/opds"):
            return httpx.Response(404)
        head = SEARCH_LINK if self.search_link else ""
        if path in ("/opds", "/opds/"):
            body = nav("Recently added", "/opds/new") + nav("Authors", "/opds/author") + nav("Series", "/opds/series")
            return xml(FEED.format(title="Calibre-Web", head=head, body=body))
        if path == "/opds/new":
            if query.get("offset") == "2":
                return xml(FEED.format(title="Recently added", head="", body=book(3, "Cuốn thứ ba", formats=("mobi",))))
            nxt = '<link rel="next" title="Next" type="application/atom+xml;profile=opds-catalog" href="/opds/new?offset=2"/>'
            total = "<opensearch:totalResults>3</opensearch:totalResults>"
            body = (book(1, "Hành trình về phương Đông", summary=LONG_SUMMARY) + book(2, "Truyện tranh", formats=("cbz",)))
            return xml(FEED.format(title="Recently added", head=head + nxt + total, body=body))
        if path.startswith("/opds/search/"):
            term = path.removeprefix("/opds/search/")
            return xml(FEED.format(title=f"Search: {escape(term)}", head="", body=book(1, "Hành trình về phương Đông")))
        if path in ("/opds/cover/1", "/opds/thumb/1"):
            return httpx.Response(200, content=b"\xff\xd8\xff\xe0jpg", headers={"content-type": "image/jpeg"})
        if path == "/opds/download/1/epub/":
            return httpx.Response(200, content=self.epub, headers={"content-type": "application/epub+zip"})
        if path == "/opds/download/9/epub/":
            return httpx.Response(200, content=b"")
        return httpx.Response(404)


def xml(text):
    return httpx.Response(200, text=text, headers={"content-type": "application/atom+xml;charset=utf-8"})


def client_for(fake, user=USER, password=PASSWORD):
    http = httpx.Client(transport=httpx.MockTransport(fake))
    return CalibreClient(CalibreConfig(BASE, user, password), http)


def test_the_front_page_lists_sections_to_open(tmp_path):
    feed = client_for(FakeCalibre()).feed()
    assert feed["title"] == "Calibre-Web" and feed["search"] == "/opds/search/{searchTerms}"
    assert [(e["kind"], e["title"], e["path"]) for e in feed["entries"]] == [
        ("nav", "Recently added", "/opds/new"), ("nav", "Authors", "/opds/author"), ("nav", "Series", "/opds/series")]


def test_a_book_shows_its_title_authors_cover_and_the_formats_this_app_can_read():
    feed = client_for(FakeCalibre()).feed("/opds/new")
    first, comics = feed["entries"]
    assert first["kind"] == "book" and first["title"] == "Hành trình về phương Đông" and first["authors"] == ["Baird T. Spalding"]
    assert [f["format"] for f in first["formats"]] == ["epub", "pdf"] and first["formats"][0]["path"] == "/opds/download/1/epub/"
    assert first["formats"][0]["size"] == 1234 and first["cover"] == "/opds/thumb/1"
    assert len(first["summary"]) <= 281 and first["summary"].endswith("…")
    assert comics["formats"] == [] and comics["unsupported"] == ["cbz"]
    assert feed["next"] == "/opds/new?offset=2" and feed["total"] == 3


def test_the_next_page_continues_the_list():
    page = client_for(FakeCalibre()).feed("/opds/new?offset=2")
    assert [e["title"] for e in page["entries"]] == ["Cuốn thứ ba"] and page["entries"][0]["formats"][0]["format"] == "mobi"
    assert page["next"] is None


def test_search_uses_the_link_the_catalogue_gives_and_escapes_the_words():
    fake = FakeCalibre()
    feed = client_for(fake).feed(query="phương đông/ & more")
    assert feed["title"].startswith("Search:") and fake.requests[-1].url.path.startswith("/opds/search/")
    raw = fake.requests[-1].url.raw_path
    assert b"%2F" in raw and b"%26" in raw and b" " not in raw and b"&" not in raw


def test_search_falls_back_to_calibre_webs_usual_address_when_the_catalogue_names_none():
    fake = FakeCalibre(search_link=False)
    assert client_for(fake).feed(query="abc")["entries"]
    assert fake.requests[-1].url.path == "/opds/search/abc"


def test_every_failure_kind_has_a_plain_message():
    with pytest.raises(CalibreError, match="rejected the username or password"):
        client_for(FakeCalibre(), password="wrong").feed()
    with pytest.raises(CalibreError, match="Enable OPDS"):
        client_for(FakeCalibre(opds=False)).feed()
    with pytest.raises(CalibreError, match="login page"):
        client_for(lambda r: httpx.Response(302, headers={"location": "/login"})).feed()
    with pytest.raises(CalibreError, match="not return an OPDS"):
        client_for(lambda r: httpx.Response(200, text="<html><body>hello</body></html>")).feed()
    with pytest.raises(CalibreError, match="not return an OPDS"):
        client_for(lambda r: httpx.Response(200, text="not xml at all")).feed()

    def down(request):
        raise httpx.ConnectError("no route")

    with pytest.raises(CalibreError, match="cannot reach Calibre-Web"):
        client_for(down).feed()
    with pytest.raises(CalibreError, match="Settings first"):
        CalibreClient(CalibreConfig())


@pytest.mark.parametrize("bad", ["http://evil.test/x", "//evil.test/x", "opds/new", "\\evil", ""])
def test_paths_that_could_leave_the_calibre_server_are_refused(bad):
    fake = FakeCalibre()
    with pytest.raises(CalibreError, match="not a path"):
        client_for(fake).feed(bad or "x")
    with pytest.raises(CalibreError):
        client_for(fake).image(bad or "x")
    assert fake.requests == []


def test_a_server_behind_a_path_prefix_is_browsed_from_its_own_front_page():
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return xml(FEED.format(title="Behind a proxy", head="", body=""))

    http = httpx.Client(transport=httpx.MockTransport(handler))
    CalibreClient(CalibreConfig("http://calibre.test:8083/library/", None, None), http).feed()
    assert seen == ["/library/opds/"]


def test_a_download_is_saved_with_its_md5_and_checked(tmp_path):
    data = b"PK" + os.urandom(4000)
    c = client_for(FakeCalibre(data))
    out = tmp_path / "b.epub"
    assert c.download("/opds/download/1/epub/", out) == hashlib.md5(data).hexdigest() and out.read_bytes() == data
    with pytest.raises(CalibreError, match="larger than"):
        c.download("/opds/download/1/epub/", tmp_path / "big.epub", max_bytes=100)
    with pytest.raises(CalibreError, match="empty file"):
        c.download("/opds/download/9/epub/", tmp_path / "empty.epub")


def test_the_test_summary_names_the_catalogue_and_its_sections():
    summary = client_for(FakeCalibre()).test()
    assert summary == {"ok": True, "title": "Calibre-Web", "sections": ["Recently added", "Authors", "Series"], "books_on_first_page": 0}


def test_the_saved_password_file_is_private_and_missing_files_mean_not_configured(tmp_path):
    store = CalibreStore(tmp_path / "config" / "calibre.json")
    assert store.load() == CalibreConfig() and not store.load().configured
    store.save(CalibreConfig(BASE, USER, PASSWORD))
    assert store.load() == CalibreConfig(BASE, USER, PASSWORD) and stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


# --- through the HTTP API ------------------------------------------------------------------

@pytest.fixture
def web(settings, store, make_epub):
    epub = make_epub("from-calibre.epub").read_bytes()
    fake = FakeCalibre(epub)
    http = httpx.Client(transport=httpx.MockTransport(fake))
    client = TestClient(create_app(settings, store, FakePool(), calibre_http=http))
    client.fake, client.epub = fake, epub
    return client


def connect(c, **kw):
    return c.put("/api/calibre", json={"url": "calibre.test:8083/", "username": USER, "password": PASSWORD, **kw})


def test_nothing_is_configured_at_first_and_browsing_says_to_connect(web):
    assert web.get("/api/calibre").json() == {"configured": False, "url": None, "username": None, "has_password": False}
    assert web.get("/api/calibre/feed").status_code == 409
    assert "Settings" in web.get("/api/calibre/feed").json()["detail"]


def test_saving_normalises_the_address_and_never_returns_the_password(web, settings):
    r = connect(web)
    assert r.status_code == 200 and r.json() == {"configured": True, "url": "http://calibre.test:8083", "username": USER, "has_password": True}
    assert PASSWORD not in r.text and PASSWORD not in web.get("/api/calibre").text
    assert stat.S_IMODE(os.stat(settings.calibre_config).st_mode) == 0o600
    assert web.put("/api/calibre", json={"url": "ftp://x"}).status_code == 422


def test_a_blank_password_keeps_the_saved_one_for_the_same_account_only(web, settings):
    connect(web)
    web.put("/api/calibre", json={"url": "http://calibre.test:8083", "username": USER})
    assert CalibreStore(settings.calibre_config).load().password == PASSWORD
    web.put("/api/calibre", json={"url": "http://calibre.test:8083", "username": "someone-else"})
    assert CalibreStore(settings.calibre_config).load().password is None
    connect(web)
    web.put("/api/calibre", json={"url": "http://calibre.test:8083", "username": USER, "clear_password": True})
    assert CalibreStore(settings.calibre_config).load().password is None
    connect(web)
    web.put("/api/calibre", json={"url": "http://other.test:8083", "username": USER})
    assert CalibreStore(settings.calibre_config).load().password is None


def test_an_empty_address_disconnects(web, settings):
    connect(web)
    assert web.put("/api/calibre", json={"url": ""}).json()["configured"] is False
    assert not CalibreStore(settings.calibre_config).load().configured


def test_test_connection_works_with_typed_or_saved_credentials_and_reports_a_wrong_one(web):
    typed = web.post("/api/calibre/test", json={"url": BASE, "username": USER, "password": PASSWORD})
    assert typed.status_code == 200 and typed.json()["title"] == "Calibre-Web"
    wrong = web.post("/api/calibre/test", json={"url": BASE, "username": USER, "password": "nope"})
    assert wrong.status_code == 502 and "rejected the username or password" in wrong.json()["detail"]
    connect(web)
    saved = web.post("/api/calibre/test", json={"url": BASE, "username": USER})
    assert saved.status_code == 200
    other_user = web.post("/api/calibre/test", json={"url": BASE, "username": "x"})
    assert other_user.status_code == 502
    assert web.post("/api/calibre/test", json={"url": "nonsense://"}).status_code == 422


def test_browsing_searching_and_pictures_go_through_the_server(web):
    connect(web)
    feed = web.get("/api/calibre/feed", params={"path": "/opds/new"}).json()
    assert [e["title"] for e in feed["entries"]] == ["Hành trình về phương Đông", "Truyện tranh"]
    found = web.get("/api/calibre/feed", params={"q": "phương đông"}).json()
    assert found["entries"][0]["kind"] == "book"
    img = web.get("/api/calibre/image", params={"path": "/opds/thumb/1"})
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg" and img.content.startswith(b"\xff\xd8\xff")
    assert web.get("/api/calibre/image", params={"path": "http://evil.test/x.jpg"}).status_code == 502


def test_a_wrong_saved_password_is_a_502_with_the_reason(web):
    connect(web, password="wrong")
    r = web.get("/api/calibre/feed")
    assert r.status_code == 502 and "rejected" in r.json()["detail"]


def test_importing_a_book_makes_an_upload_that_the_convert_flow_can_use(web):
    connect(web)
    r = web.post("/api/calibre/import", json={"path": "/opds/download/1/epub/", "title": "Hành trình / về", "format": "epub"})
    assert r.status_code == 201
    body = r.json()
    assert body["filename"].endswith(".epub") and "/" not in body["filename"]
    assert body["md5"] == hashlib.md5(web.epub).hexdigest()
    info = web.get(f"/api/uploads/{body['id']}").json()
    assert info["kind"] == "epub" and info["md5"] == body["md5"] and info["processors"]
    preview = web.post(f"/api/uploads/{body['id']}/preview", json={"processor": "auto"}).json()
    assert preview["md5"] == body["md5"] and preview["chapters"]


def test_import_refuses_unreadable_formats_and_leaves_nothing_behind_on_failure(web, settings):
    connect(web)
    assert web.post("/api/calibre/import", json={"path": "/opds/download/1/cbz/", "title": "x", "format": "cbz"}).status_code == 415
    r = web.post("/api/calibre/import", json={"path": "/opds/download/9/epub/", "title": "x", "format": "epub"})
    assert r.status_code == 502 and "empty file" in r.json()["detail"]
    r = web.post("/api/calibre/import", json={"path": "/opds/download/404/epub/", "title": "x", "format": "epub"})
    assert r.status_code == 502
    assert [p for p in settings.uploads.iterdir()] == []


# --- the cover Calibre-Web shows, not the one inside the file --------------------------------

import subprocess

from app.books.source import chosen_cover, load_source as load_book_source
from app.library import Library
from app.m4b import encode_chapter, probe_seconds, replace_cover
from app.model import Cover
from app.sources import adopt


def real_jpeg(tmp_path, color, name=None):
    """A small valid JPEG, made with ffmpeg, so tests can check pictures end to end."""
    path = tmp_path / (name or f"{color}.jpg")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c={color}:s=48x64", "-frames:v", "1",
                    str(path)], check=True)
    return path.read_bytes()


def picture_of(m4a, out):
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(m4a), "-map", "0:v", "-c", "copy", str(out)], check=True)
    return out.read_bytes()


@pytest.fixture
def web_with_covers(settings, store, make_epub, tmp_path):
    inside, calibre = real_jpeg(tmp_path, "red"), real_jpeg(tmp_path, "blue")
    epub = make_epub("covered.epub", cover=inside).read_bytes()
    fake = FakeCalibre(epub)
    original = fake.__call__

    def handler(request):
        if request.url.path == "/opds/cover/1":
            return httpx.Response(200, content=calibre, headers={"content-type": "image/jpeg"})
        return original(request)

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = TestClient(create_app(settings, store, FakePool(), calibre_http=http))
    connect(client)
    client.inside, client.calibre = inside, calibre
    return client


def import_book(c, **extra):
    body = {"path": "/opds/download/1/epub/", "title": "Có bìa", "format": "epub", **extra}
    return c.post("/api/calibre/import", json=body)


def test_the_feed_gives_the_full_size_image_as_well_as_the_thumbnail():
    book = client_for(FakeCalibre()).feed("/opds/new")["entries"][0]
    assert book["image"] == "/opds/cover/1" and book["cover"] == "/opds/thumb/1"


def test_an_imported_book_shows_the_cover_calibre_web_shows_not_the_one_inside_the_file(web_with_covers):
    c = web_with_covers
    plain = c.get(f"/api/uploads/{import_book(c).json()['id']}/cover")
    assert plain.content == c.inside  # without Calibre-Web's cover the file's own is used
    r = import_book(c, cover="/opds/cover/1")
    assert r.status_code == 201 and r.json()["cover"] == "calibre-web" and r.json()["library_cover_updated"] is False
    shown = c.get(f"/api/uploads/{r.json()['id']}/cover")
    assert shown.status_code == 200 and shown.content == c.calibre and shown.content != c.inside


def test_the_chosen_cover_reaches_the_library_and_the_chapter_files(web_with_covers, settings, store):
    c = web_with_covers
    from app.jobs import Runner
    from conftest import FakeEngine

    up = import_book(c, cover="/opds/cover/1").json()
    job = c.post(f"/api/uploads/{up['id']}/convert", json={"processor": "auto"}).json()
    Runner(settings, store, lambda _v: FakeEngine()).run_one(store.claim_next())
    assert c.get(f"/api/library/{job['book_md5']}/cover").content == c.calibre
    folder = Library(settings.library).dir(job["book_md5"])
    chapter = next(folder.glob("*.m4a"))
    assert picture_of(chapter, folder / "out.jpg") == c.calibre


def test_importing_a_book_already_in_the_library_updates_its_cover_everywhere(web_with_covers, settings, store, tmp_path):
    c = web_with_covers
    from app.jobs import Runner
    from conftest import FakeEngine

    first = import_book(c).json()  # the file's own (red) cover
    job = c.post(f"/api/uploads/{first['id']}/convert", json={"processor": "auto"}).json()
    Runner(settings, store, lambda _v: FakeEngine()).run_one(store.claim_next())
    md5 = job["book_md5"]
    folder = Library(settings.library).dir(md5)
    chapter = next(folder.glob("*.m4a"))
    seconds = probe_seconds(chapter)
    assert c.get(f"/api/library/{md5}/cover").content == c.inside

    again = import_book(c, cover="/opds/cover/1").json()
    assert again["md5"] == md5 and again["library_cover_updated"] is True
    assert c.get(f"/api/library/{md5}/cover").content == c.calibre
    assert picture_of(chapter, tmp_path / "now.jpg") == c.calibre
    assert abs(probe_seconds(chapter) - seconds) < 0.1 and not list(folder.glob("*.part"))
    assert not (folder / "cover.png").exists()


def test_a_cover_that_cannot_be_fetched_does_not_stop_the_import(web_with_covers):
    c = web_with_covers
    r = import_book(c, cover="/opds/cover/404")
    assert r.status_code == 201 and r.json()["cover"] == "file"
    assert c.get(f"/api/uploads/{r.json()['id']}/cover").content == c.inside


def test_something_that_is_not_a_picture_is_not_used_as_a_cover(web_with_covers):
    c = web_with_covers
    r = import_book(c, cover="/opds/download/9/epub/")  # an empty body, not an image
    assert r.json()["cover"] == "file"


def test_a_chosen_cover_beside_the_file_beats_the_one_inside_it_and_a_bad_one_is_ignored(tmp_path, make_epub):
    book = make_epub("c.epub", cover=real_jpeg(tmp_path, "red"))
    inside = load_book_source(book).cover
    folder = book.parent / ".cover"
    folder.mkdir()
    (folder / "cover.jpg").write_bytes(b"not a picture")
    assert load_book_source(book).cover == inside and chosen_cover(book) is None
    blue = real_jpeg(tmp_path, "blue")
    (folder / "cover.jpg").write_bytes(blue)
    assert load_book_source(book).cover == Cover(blue, "jpg")


def test_adopting_a_book_keeps_its_chosen_cover_with_it_and_a_newer_one_replaces_the_old(tmp_path):
    books = tmp_path / "books"
    first = tmp_path / "up1"
    (first / ".cover").mkdir(parents=True)
    (first / "b.epub").write_bytes(b"same bytes")
    (first / ".cover" / "cover.jpg").write_bytes(b"first")
    md5, stored = adopt(books, first / "b.epub")
    assert (stored.parent / ".cover" / "cover.jpg").read_bytes() == b"first"
    second = tmp_path / "up2"
    (second / ".cover").mkdir(parents=True)
    (second / "b.epub").write_bytes(b"same bytes")
    (second / ".cover" / "cover.png").write_bytes(b"second")
    md5b, stored2 = adopt(books, second / "b.epub")
    assert md5b == md5 and stored2 == stored
    assert sorted(p.name for p in (stored.parent / ".cover").iterdir()) == ["cover.png"]
    third = tmp_path / "up3"
    third.mkdir()
    (third / "b.epub").write_bytes(b"same bytes")
    adopt(books, third / "b.epub")  # no cover with this copy: the stored one stays
    assert (stored.parent / ".cover" / "cover.png").exists()


def test_replacing_a_chapter_files_picture_keeps_the_audio_as_it_was(tmp_path):
    import numpy as np

    red, blue = real_jpeg(tmp_path, "red"), real_jpeg(tmp_path, "blue")
    chapter = tmp_path / "c.m4a"
    audio = (0.3 * np.sin(np.linspace(0, 440 * np.pi, 24000 * 2))).astype(np.float32)
    encode_chapter(audio, 24000, chapter, {"title": "Một", "album": "Sách"}, Cover(red, "jpg"))
    before = probe_seconds(chapter)
    replace_cover(chapter, Cover(blue, "jpg"))
    assert picture_of(chapter, tmp_path / "x.jpg") == blue and abs(probe_seconds(chapter) - before) < 0.1
    tags = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title,album", "-of", "json", str(chapter)],
                          capture_output=True, text=True, check=True).stdout
    assert "Một" in tags and "Sách" in tags


# --- opening a book in Calibre-Web --------------------------------------------------------

def test_a_book_knows_its_page_in_calibre_webs_own_interface():
    books = client_for(FakeCalibre()).feed("/opds/new")["entries"]
    assert [b["web_url"] for b in books] == [f"{BASE}/book/1", f"{BASE}/book/2"]


def test_the_page_address_keeps_a_path_prefix_the_server_lives_under():
    http = httpx.Client(transport=httpx.MockTransport(FakeCalibre()))
    c = CalibreClient(CalibreConfig(BASE + "/library/", USER, PASSWORD), http)
    assert c.feed("/opds/new")["entries"][0]["web_url"] == f"{BASE}/library/book/1"


def test_a_book_without_a_number_in_its_links_has_no_page_to_open():
    feed = FEED.format(title="x", head="", body=(
        '<entry><title>Odd</title><id>urn:uuid:9</id><link rel="http://opds-spec.org/acquisition" href="/files/odd.epub" '
        'type="application/epub+zip"/></entry>'))
    c = client_for(lambda r: xml(feed))
    assert c.feed("/opds/new")["entries"][0]["web_url"] is None


def test_the_api_passes_the_page_address_on(web):
    connect(web)
    first = web.get("/api/calibre/feed", params={"path": "/opds/new"}).json()["entries"][0]
    assert first["web_url"] == f"{BASE}/book/1"
