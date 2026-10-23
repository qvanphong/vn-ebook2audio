import json
import subprocess
import threading
from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import JobStore, Runner
from app.library import Library, md5_file
from app.model import Plan, State, Voice
from conftest import FakeEngine

SPEC = Voice("nano", None, 0.8, True)
DIALOG_SPEC = Voice("nano", None, 0.8, True, "Mỹ Duyên")


def probe(path, entries="format_tags"):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", f"{entries}=title,artist,album,track,genre",
                          "-of", "json", str(path)], capture_output=True, check=True)
    return json.loads(out.stdout)


def tags(path):
    return probe(path)["format"]["tags"]


def stream_types(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return out.stdout.split()


def runner(settings, store, engine):
    return Runner(settings, store, lambda _v: engine)


def m4a_files(settings, md5):
    return sorted(p.name for p in (settings.library / md5).glob("*.m4a"))


def test_run_writes_one_m4a_per_chapter_into_a_folder_named_by_the_books_md5(settings, store, make_epub):
    book = make_epub()
    md5 = md5_file(book)
    job = store.create(book, SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    done = store.get(job.id)
    assert done.state is State.DONE and done.book_md5 == md5
    assert done.output == str(settings.library / md5)
    files = m4a_files(settings, md5)
    assert len(files) == 2 and files[0].startswith("0001-") and files[1].startswith("0002-")
    assert not list((settings.library / md5).glob("*.m4b"))
    first = tags(settings.library / md5 / files[0])
    assert (first["title"], first["artist"], first["album"], first["track"], first["genre"]) == (
        "Chương 1", "Tác giả Thử", "Sách thử", "1", "Audiobook")


def test_manifest_records_text_hash_voice_and_timings(settings, store, make_epub):
    book = make_epub()
    md5 = md5_file(book)
    store.create(book, SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    entries = Library(settings.library).entries(md5)
    assert [e.title for e in entries] == ["Chương 1", "Chương 2"]
    assert all(len(e.text_hash) == 16 and e.seconds > 0 and e.bytes > 0 and e.model == "nano" for e in entries)
    assert entries[0].voice_sig == SPEC.signature()[:6] and entries[0].processor == "auto"
    manifest = json.loads((settings.library / md5 / "book.json").read_text(encoding="utf8"))
    assert (manifest["title"], manifest["author"], manifest["md5"]) == ("Sách thử", "Tác giả Thử", md5)


def test_job_reports_titles_files_and_lengths_per_chapter(settings, store, make_epub):
    job = store.create(make_epub(), SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    chapters = store.get(job.id).public()["chapters"]
    assert [c["title"] for c in chapters] == ["Chương 1", "Chương 2"]
    assert all(c["seconds"] > 0 and c["file"].endswith(".m4a") for c in chapters)


def test_failed_run_keeps_finished_chapters_and_retry_reuses_them(settings, store, make_epub):
    book = make_epub()
    md5 = md5_file(book)
    job = store.create(book, SPEC)
    runner(settings, store, FakeEngine(fail_on="Sáng hôm sau")).run_one(store.claim_next())
    failed = store.get(job.id)
    assert failed.state is State.ERROR and failed.chapters_done == 1 and "boom" in failed.error
    assert len(m4a_files(settings, md5)) == 1

    store.retry(job.id)
    second = FakeEngine()
    runner(settings, store, second).run_one(store.claim_next())
    assert store.get(job.id).state is State.DONE and len(m4a_files(settings, md5)) == 2
    assert not any("Anh Nam" in c for c in second.calls) and any("Sáng hôm sau" in c for c in second.calls)


def test_chapters_that_already_have_audio_in_the_same_voice_are_skipped_unless_overwrite(settings, store, make_epub):
    book = make_epub()
    store.create(book, SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())

    skipped = FakeEngine()
    store.create(book, SPEC)
    runner(settings, store, skipped).run_one(store.claim_next())
    assert skipped.calls == []

    redone = FakeEngine()
    store.create(book, SPEC, Plan(overwrite=True))
    runner(settings, store, redone).run_one(store.claim_next())
    assert any("Anh Nam" in c for c in redone.calls) and any("Sáng hôm sau" in c for c in redone.calls)
    assert len(Library(settings.library).entries(md5_file(book))) == 2


def test_a_different_voice_adds_files_next_to_the_existing_ones(settings, store, make_epub):
    book = make_epub()
    md5 = md5_file(book)
    store.create(book, SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    store.create(book, Voice("nano", "Mỹ Duyên", 0.8, True))
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    entries = Library(settings.library).entries(md5)
    assert len(entries) == 4 and len({e.voice_sig for e in entries}) == 2 and len(m4a_files(settings, md5)) == 4


def test_overwrite_job_does_not_redo_chapters_it_already_finished_when_retried(settings, store, make_epub):
    book = make_epub()
    store.create(book, SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    job = store.create(book, SPEC, Plan(overwrite=True))
    runner(settings, store, FakeEngine(fail_on="Sáng hôm sau")).run_one(store.claim_next())
    assert store.get(job.id).state is State.ERROR
    store.retry(job.id)
    again = FakeEngine()
    runner(settings, store, again).run_one(store.claim_next())
    assert store.get(job.id).state is State.DONE
    assert not any("Anh Nam" in c for c in again.calls) and any("Sáng hôm sau" in c for c in again.calls)


def test_interrupted_job_is_requeued_on_start(settings, store, make_epub):
    job = store.create(make_epub(), SPEC)
    store.update(job.id, state=State.RUNNING)
    assert store.requeue_interrupted() == 1
    assert store.get(job.id).state is State.QUEUED


def test_cancel_during_render_stops_but_keeps_finished_chapters(settings, store, make_epub):
    book = make_epub()
    job = store.create(book, SPEC)

    class Cancelling(FakeEngine):
        def synthesize(self, text, dialog=False):
            if "Sáng hôm sau" in text:
                store.request_cancel(job.id)
            return super().synthesize(text, dialog)

    runner(settings, store, Cancelling()).run_one(store.claim_next())
    assert store.get(job.id).state is State.CANCELLED
    assert len(m4a_files(settings, md5_file(book))) == 1


def test_queued_job_cancels_immediately_and_can_be_retried(settings, store, make_epub):
    job = store.create(make_epub(), SPEC)
    assert store.request_cancel(job.id).state is State.CANCELLED
    assert store.retry(job.id).state is State.QUEUED


def test_dialogue_clips_use_the_dialogue_voice_only_when_set(settings, store, make_epub):
    chapters = [("Chương 1", ["Anh nói: “Hôm nay tôi rất vui vẻ.” Rồi anh cười thật tươi.", "Một đoạn kể chuyện khá dài dòng cho đủ chữ. " * 6])]
    plain, voiced = FakeEngine(), FakeEngine()
    store.create(make_epub("a.epub", chapters), SPEC)
    runner(settings, store, plain).run_one(store.claim_next())
    store.create(make_epub("b.epub", chapters, title="Khác"), DIALOG_SPEC)
    runner(settings, store, voiced).run_one(store.claim_next())
    assert plain.dialog_calls == []
    assert voiced.dialog_calls == ["Hôm nay tôi rất vui vẻ."]


def test_changing_the_dialogue_voice_changes_the_voice_signature():
    assert SPEC.signature() != DIALOG_SPEC.signature()


def test_bad_book_becomes_error_with_message(settings, store, tmp_path):
    bad = tmp_path / "bad.epub"
    bad.write_bytes(b"not a zip")
    job = store.create(bad, SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    got = store.get(job.id)
    assert got.state is State.ERROR and got.error


def test_runner_thread_drains_queue_and_stops(settings, store, make_epub):
    job = store.create(make_epub(), SPEC)
    r = Runner(settings, store, lambda _v: FakeEngine(), idle_seconds=0.05)
    r.start()
    for _ in range(200):
        if store.get(job.id).state is State.DONE:
            break
        threading.Event().wait(0.05)
    r.stop(); r.join(2)
    assert store.get(job.id).state is State.DONE


def test_cover_is_saved_in_the_book_folder_and_embedded_in_each_chapter(settings, store, make_epub):
    import base64

    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    book = make_epub(cover=png)
    md5 = md5_file(book)
    store.create(book, SPEC)
    runner(settings, store, FakeEngine()).run_one(store.claim_next())
    folder = settings.library / md5
    assert (folder / "cover.png").exists()
    assert "video" in stream_types(folder / m4a_files(settings, md5)[0])


def test_relative_data_dir_still_works(tmp_path, monkeypatch, make_epub):
    book = make_epub()
    monkeypatch.chdir(tmp_path)
    relative = Settings(data_dir=Path("data"), settle_seconds=0.0)
    relative.ensure_dirs()
    local_store = JobStore(relative.db)
    job = local_store.create(book, SPEC)
    Runner(relative, local_store, lambda _v: FakeEngine(), Library(relative.library.resolve())).run_one(local_store.claim_next())
    done = local_store.get(job.id)
    assert done.state is State.DONE, done.error
    assert len(list(Path(done.output).glob("*.m4a"))) == 2


def test_settings_from_env_makes_the_data_dir_absolute(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", "./data")
    assert Settings.from_env().data_dir == (tmp_path / "data").resolve()
