import json
import subprocess

from app.jobs import Runner
from app.library import md5_file
from conftest import LONG, FakeEngine
from test_flow import make_client, three_chapter_epub, upload


def convert_and_run(client, settings, store, path, body=None, engine=None):
    uid = upload(client, path)
    r = client.post(f"/api/uploads/{uid}/convert", json={"processor": "epub-toc", "selection": [1, 2, 3], **(body or {})})
    assert r.status_code == 201, r.text
    engine = engine or FakeEngine()
    Runner(settings, store, lambda _v: engine).run_one(store.claim_next())
    return r.json(), engine


def test_upload_reports_the_books_md5_and_how_much_audio_it_already_has(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    uid = upload(c, path)
    info = c.get(f"/api/uploads/{uid}").json()
    assert info["md5"] == md5_file(path) and info["audio_count"] == 0
    convert_and_run(c, settings, store, path)
    again = c.get(f"/api/uploads/{upload(c, path)}").json()
    assert again["md5"] == info["md5"] and again["audio_count"] == 3


def test_preview_marks_chapters_that_already_have_audio_with_the_voice_used(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"selection": [1, 2], "model": "nano", "voice": "Mỹ Duyên"})
    preview = c.post(f"/api/uploads/{upload(c, path)}/preview", json={"processor": "epub-toc"}).json()
    audio = [ch["audio"] for ch in preview["chapters"]]
    assert [len(a) for a in audio] == [0, 1, 1, 0]
    assert audio[1][0]["voice"] == "Mỹ Duyên" and audio[1][0]["model"] == "nano" and audio[1][0]["voice_label"] == "Nano · Mỹ Duyên"
    assert preview["audio_count"] == 2 and preview["md5"] == md5_file(path)


def test_a_different_processor_split_does_not_claim_audio_it_did_not_make(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path)
    preview = c.post(f"/api/uploads/{upload(c, path)}/preview", json={"processor": "fixed-size", "params": {"chars": "400"}}).json()
    assert all(ch["audio"] == [] for ch in preview["chapters"])


def test_convert_reports_how_many_selected_chapters_already_had_audio_and_skips_them(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"selection": [1, 2]})
    job, engine = convert_and_run(c, settings, store, path, {"selection": [1, 2, 3]})
    assert job["already_had_audio"] == 2
    assert "Chương 3" in engine.calls and "Chương 1" not in engine.calls and "Chương 2" not in engine.calls
    assert len({e["file"] for e in c.get(f"/api/library/{md5_file(path)}").json()["groups"][0]["files"]}) == 3


def test_overwrite_renders_existing_chapters_again(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"selection": [1]})
    _, skipped = convert_and_run(c, settings, store, path, {"selection": [1]})
    assert skipped.calls == []
    _, redone = convert_and_run(c, settings, store, path, {"selection": [1], "overwrite": True})
    assert redone.calls != []


def test_the_same_book_uploaded_twice_is_stored_once_under_its_md5(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"selection": [1]})
    convert_and_run(c, settings, store, path, {"selection": [2]})
    assert [p.name for p in settings.books.iterdir()] == [md5_file(path)]
    assert [p.name for p in (settings.books / md5_file(path)).iterdir()] == [path.name]
    assert [p.name for p in settings.library.iterdir()] == [md5_file(path)]


def test_library_lists_books_with_counts_voices_and_total_length(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"voice": "Mỹ Duyên"})
    convert_and_run(c, settings, store, path, {"voice": None, "selection": [1]})
    (book,) = c.get("/api/library").json()
    assert book["md5"] == md5_file(path) and book["title"] == "Sách thật" and book["audio_count"] == 4
    assert sorted(book["voices"]) == ["Nano · Mỹ Duyên", "Nano · default voice"] and book["seconds"] > 0
    assert c.get("/api/library").json()[0]["has_cover"] is False


def test_library_detail_groups_audio_by_voice_in_chapter_order(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"voice": "Mỹ Duyên"})
    detail = c.get(f"/api/library/{md5_file(path)}").json()
    (group,) = detail["groups"]
    assert group["label"] == "Nano · Mỹ Duyên" and [f["title"] for f in group["files"]] == ["Chương 1", "Chương 2", "Chương 3"]
    assert group["seconds"] > 0 and detail["md5"] == md5_file(path) and detail["author"] == "Dr. Blair T. Spalding"


def test_library_serves_chapter_audio_with_ranges_and_refuses_other_files(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path)
    md5 = md5_file(path)
    name = c.get(f"/api/library/{md5}").json()["groups"][0]["files"][0]["file"]
    full = c.get(f"/api/library/{md5}/audio/{name}")
    assert full.status_code == 200 and full.headers["content-type"] == "audio/mp4" and full.headers["content-disposition"].startswith("inline")
    part = c.get(f"/api/library/{md5}/audio/{name}", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert c.get(f"/api/library/{md5}/audio/{name}", params={"download": "true"}).headers["content-disposition"].startswith("attachment")
    for bad in ("book.json", "..%2Fbook.json", "nope.m4a"):
        assert c.get(f"/api/library/{md5}/audio/{bad}").status_code == 404
    assert c.get("/api/library/not-an-md5/audio/x.m4a").status_code == 404


def test_library_deletes_one_file_or_the_whole_book(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path)
    md5 = md5_file(path)
    name = c.get(f"/api/library/{md5}").json()["groups"][0]["files"][0]["file"]
    assert c.delete(f"/api/library/{md5}/audio/{name}").status_code == 204
    assert c.delete(f"/api/library/{md5}/audio/{name}").status_code == 404
    assert len(c.get(f"/api/library/{md5}").json()["groups"][0]["files"]) == 2
    preview = c.post(f"/api/uploads/{upload(c, path)}/preview", json={"processor": "epub-toc"}).json()
    assert [len(ch["audio"]) for ch in preview["chapters"]] == [0, 0, 1, 1]
    assert c.delete(f"/api/library/{md5}").status_code == 204
    assert c.get(f"/api/library/{md5}").status_code == 404 and c.get("/api/library").json() == []


def test_single_m4b_export_joins_a_voice_group_with_chapter_marks_and_is_cached(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"voice": "Mỹ Duyên"})
    md5 = md5_file(path)
    sig = c.get(f"/api/library/{md5}").json()["groups"][0]["voice_sig"]
    r = c.get(f"/api/library/{md5}/book.m4b", params={"group": sig})
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    out = next(settings.m4b_cache.glob("*.m4b"))
    probed = subprocess.run(["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(out)],
                            capture_output=True, text=True, check=True).stdout
    assert [c["tags"]["title"] for c in json.loads(probed)["chapters"]] == ["Chương 1", "Chương 2", "Chương 3"]
    stamp = out.stat().st_mtime_ns
    c.get(f"/api/library/{md5}/book.m4b", params={"group": sig})
    assert next(settings.m4b_cache.glob("*.m4b")).stat().st_mtime_ns == stamp
    assert c.get(f"/api/library/{md5}/book.m4b", params={"group": "zzzzzz"}).status_code == 404
