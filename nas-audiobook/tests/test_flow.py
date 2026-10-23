import os
import time

from fastapi.testclient import TestClient

from app.api import create_app
from app.jobs import Runner
from app.library import Library
from app.model import State
from app.uploads import UploadStore
from conftest import LONG, FakeEngine
from test_api import FakePool


def make_client(settings, store, pool=None):
    pool = pool or FakePool()
    return TestClient(create_app(settings, store, pool)), pool


def three_chapter_epub(toc_epub):
    files = {"front.html": "<p>Chào mừng</p>", "c1.html": f"<p>{LONG}</p>", "c2.html": f"<p>{LONG}</p>", "c3.html": f"<p>{LONG}</p>"}
    toc = [("Lời chào", "front.html"), ("Chương 1", "c1.html"), ("Chương 2", "c2.html"), ("Chương 3", "c3.html")]
    return toc_epub(files, toc)


def upload(client, path, name=None):
    with open(path, "rb") as f:
        r = client.post("/api/uploads", files={"file": (name or path.name, f)})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_processor_list_exposes_labels_and_parameters(settings, store):
    c, _ = make_client(settings, store)
    procs = {p["id"]: p for p in c.get("/api/processors").json()}
    assert list(procs) == ["auto", "epub-toc", "epub-spine", "epub-headings", "text-pattern", "fixed-size"]
    assert procs["fixed-size"]["params"][0]["kind"] == "number"
    assert procs["epub-headings"]["params"][0]["choices"] == ["h1", "h1-h2", "h1-h3"]


def test_upload_info_lists_only_the_processors_that_can_read_the_file(settings, store, toc_epub, tmp_path):
    c, _ = make_client(settings, store)
    info = c.get(f"/api/uploads/{upload(c, three_chapter_epub(toc_epub))}").json()
    assert info["kind"] == "epub" and info["title"] == "Sách thật" and len(info["processors"]) == 6
    txt = tmp_path / "a.txt"; txt.write_text("x")
    assert [p["id"] for p in c.get(f"/api/uploads/{upload(c, txt)}").json()["processors"]] == ["auto", "text-pattern", "fixed-size"]


def test_upload_rejects_unsupported_files_and_unknown_ids(settings, store):
    c, _ = make_client(settings, store)
    assert c.post("/api/uploads", files={"file": ("x.exe", b"x")}).status_code == 415
    assert c.get("/api/uploads/nope").status_code == 404
    assert c.post("/api/uploads/nope/preview", json={}).status_code == 404


def test_preview_lists_chapters_with_size_boundaries_and_default_selection(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, three_chapter_epub(toc_epub))
    preview = c.post(f"/api/uploads/{uid}/preview", json={"processor": "epub-toc"}).json()
    assert [ch["title"] for ch in preview["chapters"]] == ["Lời chào", "Chương 1", "Chương 2", "Chương 3"]
    assert [ch["selected"] for ch in preview["chapters"]] == [False, True, True, True]
    first = preview["chapters"][1]
    assert first["chars"] > 200 and first["seconds"] > 0 and first["head"].startswith("Một câu đủ dài")
    auto = c.post(f"/api/uploads/{uid}/preview", json={}).json()
    assert auto["processor"] == "auto" and "table of contents" in auto["note"]


def test_switching_processor_changes_the_split(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, three_chapter_epub(toc_epub))
    toc = c.post(f"/api/uploads/{uid}/preview", json={"processor": "epub-toc"}).json()["chapters"]
    fixed = c.post(f"/api/uploads/{uid}/preview", json={"processor": "fixed-size", "params": {"chars": "400"}}).json()["chapters"]
    assert len(toc) == 4 and [ch["title"] for ch in fixed][:2] == ["Part 1", "Part 2"] and len(fixed) != len(toc)


def test_preview_reports_bad_settings_as_422(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, three_chapter_epub(toc_epub))
    for body in ({"processor": "text-pattern", "params": {"pattern": "("}}, {"processor": "nope"},
                 {"processor": "fixed-size", "params": {"chars": "0"}}):
        r = c.post(f"/api/uploads/{uid}/preview", json=body)
        assert r.status_code == 422 and r.json()["detail"]


def test_convert_only_renders_selected_chapters_and_applies_overrides(settings, store, toc_epub):
    c, pool = make_client(settings, store)
    uid = upload(c, three_chapter_epub(toc_epub))
    r = c.post(f"/api/uploads/{uid}/convert", json={"processor": "epub-toc", "selection": [1, 3], "title": "Tên mới",
                                                    "author": "Tác giả mới", "model": "nano", "voice": "Mỹ Duyên"})
    assert r.status_code == 201 and r.json()["processor"] == "epub-toc" and r.json()["selected"] == 2
    assert c.get(f"/api/uploads/{uid}").status_code == 404
    Runner(settings, store, lambda _v: FakeEngine()).run_one(store.claim_next())
    done = store.get(r.json()["id"])
    assert done.state is State.DONE
    entries = Library(settings.library).entries(done.book_md5)
    assert [e.title for e in entries] == ["Chương 1", "Chương 3"]
    manifest = Library(settings.library).manifest(done.book_md5)
    assert (manifest["title"], manifest["author"]) == ("Tên mới", "Tác giả mới")


def test_convert_without_selection_uses_the_default_and_empty_selection_is_refused(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, three_chapter_epub(toc_epub))
    assert c.post(f"/api/uploads/{uid}/convert", json={"selection": []}).status_code == 422
    r = c.post(f"/api/uploads/{uid}/convert", json={})
    assert r.status_code == 201 and r.json()["selected"] == 3



def test_voice_sample_is_audio_and_cached(settings, store):
    c, pool = make_client(settings, store)
    r = c.get("/api/voice-sample", params={"model": "nano", "voice": "Mỹ Duyên"})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content[:3] in (b"ID3", b"\xff\xfb", b"\xff\xf3")
    c.get("/api/voice-sample", params={"model": "nano", "voice": "Mỹ Duyên"})
    assert len(pool.synth_calls) == 1
    c.get("/api/voice-sample", params={"model": "nano", "voice": "Mỹ Duyên", "text": "Câu khác."})
    assert len(pool.synth_calls) == 2 and pool.synth_calls[1][1] == "Câu khác."


def test_voice_sample_validation_and_loading_states(settings, store):
    c, pool = make_client(settings, store)
    assert c.get("/api/voice-sample", params={"model": "nano", "text": "x" * 400}).status_code == 400
    assert c.get("/api/voice-sample", params={"model": "huge"}).status_code == 400
    pool.loading = "wait"
    r = c.get("/api/voice-sample", params={"model": "nano"})
    assert r.status_code == 503 and r.json()["detail"]["loading"] is True


def test_stale_uploads_are_swept(settings):
    uploads = UploadStore(settings.uploads)
    old, fresh = uploads.create("a.txt"), uploads.create("b.txt")
    old.path.write_text("x"); fresh.path.write_text("x")
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old.path.parent, (long_ago, long_ago))
    assert uploads.sweep() == 1 and uploads.get(old.id) is None and uploads.get(fresh.id) is not None



def test_running_job_lists_titles_before_any_chapter_is_done(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, three_chapter_epub(toc_epub))
    jid = c.post(f"/api/uploads/{uid}/convert", json={"processor": "epub-toc", "selection": [1, 2, 3]}).json()["id"]
    seen = {}

    class Peek(FakeEngine):
        def synthesize(self, text, dialog=False):
            seen.setdefault("chapters", c.get(f"/api/jobs/{jid}").json()["chapters"])
            return super().synthesize(text, dialog)

    Runner(settings, store, lambda _v: Peek()).run_one(store.claim_next())
    assert [ch["title"] for ch in seen["chapters"]] == ["Chương 1", "Chương 2", "Chương 3"]
    assert all(ch["seconds"] is None for ch in seen["chapters"])
