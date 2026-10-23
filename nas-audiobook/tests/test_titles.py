from app.jobs import Runner
from app.library import md5_file
from conftest import LONG, FakeEngine
from test_flow import make_client, upload

BODY = "Nội dung rất dài để vượt ngưỡng tối thiểu của một chương. " * 8


def run_all(settings, store):
    while (job := store.claim_next()) is not None:
        Runner(settings, store, lambda _v: FakeEngine()).run_one(job)


def test_quick_path_text_book_is_titled_by_its_file_name_not_its_md5(settings, store):
    c, _ = make_client(settings, store)
    r = c.post("/api/jobs", files={"file": ("Truyện của tôi.txt", f"Chương 1\n{BODY}\nChương 2\n{BODY}".encode())})
    assert r.status_code == 201
    run_all(settings, store)
    (book,) = c.get("/api/library").json()
    assert book["title"] == "Truyện của tôi" and book["md5"] not in book["title"]


def test_inbox_book_is_titled_by_its_file_name(settings, store):
    from app.watcher import scan_inbox

    (settings.inbox / "Sách trong hộp thư.txt").write_text(f"Chương 1\n{BODY}\nChương 2\n{BODY}", encoding="utf8")
    assert scan_inbox(settings, store) == 1
    run_all(settings, store)
    (book,) = Runner(settings, store, lambda _v: FakeEngine()).library.list_books()
    assert book["title"] == "Sách trong hộp thư"


def test_epub_without_title_metadata_falls_back_to_the_file_name(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = toc_epub({"a.html": f"<p>{LONG}</p>", "b.html": f"<p>{LONG}</p>"}, [("Một", "a.html"), ("Hai", "b.html")], title="")
    c.post("/api/jobs", files={"file": ("Cuốn sách không tên.epub", path.read_bytes())})
    run_all(settings, store)
    (book,) = c.get("/api/library").json()
    assert book["title"] == "Cuốn sách không tên"


def test_rename_changes_the_library_listing_the_manifest_and_every_chapter_tag(settings, store, toc_epub):
    import json
    import subprocess

    from test_flow import three_chapter_epub

    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    md5 = md5_file(path)
    uid = upload(c, path)
    c.post(f"/api/uploads/{uid}/convert", json={"processor": "epub-toc", "selection": [1, 2], "voice": "Mỹ Duyên"})
    run_all(settings, store)
    before = c.get(f"/api/library/{md5}").json()["groups"][0]["files"]

    r = c.patch(f"/api/library/{md5}", json={"title": "Tên đã sửa", "author": "Người dịch"})
    assert r.status_code == 200 and (r.json()["title"], r.json()["author"]) == ("Tên đã sửa", "Người dịch")
    (book,) = c.get("/api/library").json()
    assert (book["title"], book["author"], book["md5"]) == ("Tên đã sửa", "Người dịch", md5)
    for f in before:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:format_tags=album,artist,title",
                              "-of", "json", str(settings.library / md5 / f["file"])], capture_output=True, text=True, check=True)
        info = json.loads(out.stdout)["format"]
        assert (info["tags"]["album"], info["tags"]["artist"]) == ("Tên đã sửa", "Người dịch")
        assert info["tags"]["title"] == f["title"] and abs(float(info["duration"]) - f["seconds"]) < 0.2
    sig = c.get(f"/api/library/{md5}").json()["groups"][0]["voice_sig"]
    named = c.get(f"/api/library/{md5}/book.m4b", params={"group": sig}).headers["content-disposition"]
    assert "T%C3%AAn" in named


def test_rename_validates_and_needs_an_existing_book(settings, store):
    c, _ = make_client(settings, store)
    assert c.patch("/api/library/" + "0" * 32, json={"title": "x"}).status_code == 404
    assert c.patch("/api/library/not-md5", json={"title": "x"}).status_code == 404


def test_renamed_book_keeps_its_name_when_uploaded_again(settings, store, toc_epub):
    from test_flow import three_chapter_epub

    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    uid = upload(c, path)
    assert c.get(f"/api/uploads/{uid}").json()["in_library"] is False
    c.post(f"/api/uploads/{uid}/convert", json={"processor": "epub-toc", "selection": [1]})
    run_all(settings, store)
    md5 = md5_file(path)
    assert c.patch(f"/api/library/{md5}", json={"title": "Tên đã sửa", "author": "Tác giả khác"}).status_code == 200
    info = c.get(f"/api/uploads/{upload(c, path)}").json()
    assert (info["title"], info["author"], info["in_library"]) == ("Tên đã sửa", "Tác giả khác", True)


def test_library_remembers_the_original_file_name(settings, store):
    c, _ = make_client(settings, store)
    c.post("/api/jobs", files={"file": ("Truyện của tôi.txt", f"Chương 1\n{BODY}\nChương 2\n{BODY}".encode())})
    run_all(settings, store)
    assert c.get("/api/library").json()[0]["source_name"] == "Truyện của tôi.txt"
