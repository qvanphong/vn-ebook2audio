from app.jobs import clip_batches, render_chapter
from app.model import Chapter, Clip, State, Voice
from app.library import md5_file
from conftest import FakeEngine
from test_jobs import SPEC, m4a_files, runner

from fastapi.testclient import TestClient

from app.api import create_app


def test_pausing_a_queued_job_keeps_it_out_of_the_queue_until_it_is_resumed(settings, store, make_epub):
    job = store.create(make_epub(), SPEC)
    assert store.request_pause(job.id).state is State.PAUSED
    assert store.claim_next() is None
    assert store.retry(job.id).state is State.QUEUED


def test_pausing_a_running_job_stops_it_as_paused_and_resume_reuses_finished_chapters(settings, store, make_epub):
    book = make_epub()
    job = store.create(book, SPEC)

    class Pausing(FakeEngine):
        def synthesize(self, text, dialog=False):
            if "Sáng hôm sau" in text:
                store.request_pause(job.id)
            return super().synthesize(text, dialog)

    runner(settings, store, Pausing()).run_one(store.claim_next())
    paused = store.get(job.id)
    assert paused.state is State.PAUSED and paused.cancel is False and paused.pause is False
    assert len(m4a_files(settings, md5_file(book))) == 1

    resumed = store.retry(job.id)
    assert resumed.state is State.QUEUED
    engine = FakeEngine()
    runner(settings, store, engine).run_one(store.claim_next())
    assert store.get(job.id).state is State.DONE
    assert len(m4a_files(settings, md5_file(book))) == 2
    assert engine.calls and not any("Anh Nam" in text for text in engine.calls)


def test_clear_removes_only_finished_stopped_jobs_and_keeps_active_ones(settings, store, make_epub):
    done, failed, paused, queued = (store.create(make_epub(f"{n}.epub", title=f"T{n}"), SPEC) for n in range(4))
    store.update(done.id, state=State.DONE)
    store.update(failed.id, state=State.ERROR)
    store.update(paused.id, state=State.PAUSED)

    assert store.clear([State.DONE]) == 1
    assert store.get(done.id) is None and store.get(failed.id) is not None
    assert store.clear([State.ERROR, State.CANCELLED]) == 1
    assert store.clear([State.PAUSED]) == 1
    assert store.get(queued.id).state is State.QUEUED
    assert store.clear([State.RUNNING]) == 0 and store.clear([]) == 0


def test_clear_endpoint_rejects_active_states_and_reports_how_many_went(settings, store, make_epub):
    c = TestClient(create_app(settings, store, None))
    job = store.create(make_epub(), SPEC)
    store.update(job.id, state=State.DONE)
    assert c.post("/api/jobs/clear", json={"states": ["queued"]}).status_code == 422
    assert c.post("/api/jobs/clear", json={"states": ["nope"]}).status_code == 422
    assert c.post("/api/jobs/clear", json={"states": []}).status_code == 422
    assert c.post("/api/jobs/clear", json={"states": ["done", "paused"]}).json() == {"removed": 1}


def test_pause_endpoint_and_public_state(settings, store, make_epub):
    c = TestClient(create_app(settings, store, None))
    job = store.create(make_epub(), SPEC)
    body = c.post(f"/api/jobs/{job.id}/pause").json()
    assert body["state"] == "paused" and "pause" not in body and "cancel" not in body
    assert c.post(f"/api/jobs/{job.id}/retry").json()["state"] == "queued"
    assert c.post("/api/jobs/nope/pause").status_code == 404


def test_clip_batches_group_neighbours_in_the_same_voice_up_to_the_size():
    clips = [Clip("a", 0), Clip("b", 0), Clip("c", 0, dialog=True), Clip("d", 0, dialog=True), Clip("e", 0), Clip("f", 0)]
    groups = clip_batches(clips, 2)
    assert [[i for i, _ in g] for g in groups] == [[0, 1], [2, 3], [4, 5]]
    assert [[c.text for _, c in g] for g in groups] == [["a", "b"], ["c", "d"], ["e", "f"]]
    assert [[i for i, _ in g] for g in clip_batches(clips, 1)] == [[i] for i in range(6)]
    assert clip_batches([], 4) == []


def test_render_sends_neighbouring_clips_together_and_keeps_the_order_and_pauses():
    clips = tuple(Clip(f"câu {i}", pause_ms=100 * i, dialog=i in (2, 3)) for i in range(5))
    chapter = Chapter("Chương", clips)
    engine = FakeEngine(batch=3)
    checks, progress = [], []
    parts = render_chapter(chapter, engine, 1000, lambda: checks.append(1), lambda i, n: progress.append((i, n)))
    assert engine.requests == [["câu 0", "câu 1"], ["câu 2", "câu 3"], ["câu 4"]]
    assert engine.dialog_calls == ["câu 2", "câu 3"]
    assert progress == [(0, 5), (2, 5), (4, 5)] and len(checks) == 3
    assert len(parts) == 2 * len(clips)
    assert [len(p) for p in parts[1::2]] == [100 * i for i in range(5)]


def test_batch_of_one_makes_one_request_per_clip_as_before():
    clips = tuple(Clip(f"c{i}", 0) for i in range(3))
    engine = FakeEngine(batch=1)
    render_chapter(Chapter("x", clips), engine, 1000, lambda: None, lambda i, n: None)
    assert engine.requests == [["c0"], ["c1"], ["c2"]]


def test_voice_engine_clamps_the_batch_size():
    from app.tts import MAX_BATCH, VieneuEngine

    class Pool:
        def get(self, model):
            return None

    spec = Voice("nano", None, 0.8, True)
    assert VieneuEngine(Pool(), spec, batch=0).batch == 1
    assert VieneuEngine(Pool(), spec, batch=1000).batch == MAX_BATCH
    assert VieneuEngine(Pool(), spec).batch == 1
