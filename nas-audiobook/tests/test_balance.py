"""A conversion spread over several machines: chapters go to whichever lane is free, a lane that fails drops out
and hands its chapter back, and nothing waits for a machine that is still downloading its model."""
import threading
import time

import pytest

from app.jobs import Runner
from app.library import Library, md5_file
from app.model import Plan, State, Voice
from app.remote import WorkerError
from app.tts import LaneError
from conftest import FILLER, FakeEngine

SPEC = Voice("nano", None, 0.8, True)


def chapters(count=6):
    return [(f"Chương {k}", [f"Mốc số {k} nằm ở đây.", FILLER]) for k in range(1, count + 1)]


class FakeLane:
    def __init__(self, id, engine=None, prepare_error=None, gate=None):
        self.id, self.label = id, f"Máy {id}"
        self._engine = engine or FakeEngine()
        self.prepare_error, self.gate = prepare_error, gate
        self.prepared = 0

    def engine(self, spec, report=lambda _m: None):
        self.prepared += 1
        report("loading the model")
        if self.gate is not None:
            self.gate.wait(30)
        if self.prepare_error:
            raise self.prepare_error
        return self._engine


class Source:
    def __init__(self, *lanes):
        self.all = list(lanes)

    def lanes(self, selected):
        return [l for l in self.all if selected is None or l.id in selected]


class Rendezvous(FakeEngine):
    """Blocks its first call until every lane has one in flight, which only happens if they run at the same time."""

    def __init__(self, barrier, seen=None, **kw):
        super().__init__(**kw)
        self.barrier, self.first, self.seen = barrier, True, seen

    def synthesize(self, text, dialog=False):
        if self.first:
            self.first = False
            self.barrier.wait(5)
            if self.seen is not None:
                self.seen()
        return super().synthesize(text, dialog)


def run(settings, store, make_epub, *lanes, plan=None, count=6, name="book.epub"):
    book = make_epub(name, chapters(count))
    job = store.create(book, SPEC, plan)
    runner = Runner(settings, store, Source(*lanes))
    runner.run_one(store.claim_next())
    return store.get(job.id), md5_file(book), runner


def indexes(settings, md5):
    return sorted(e.index for e in Library(settings.library).entries(md5))


def test_chapters_are_rendered_at_the_same_time_on_every_lane(settings, store, make_epub):
    barrier = threading.Barrier(2)
    a, b = FakeLane("a", Rendezvous(barrier)), FakeLane("b", Rendezvous(barrier))
    job, md5, _ = run(settings, store, make_epub, a, b)
    assert job.state is State.DONE and job.chapters_done == 6
    assert indexes(settings, md5) == [0, 1, 2, 3, 4, 5]
    assert a._engine.calls and b._engine.calls


def test_every_chapter_is_rendered_exactly_once_and_the_job_reports_each_machine(settings, store, make_epub):
    a, b = FakeLane("a"), FakeLane("b")
    job, md5, _ = run(settings, store, make_epub, a, b, count=8)
    assert indexes(settings, md5) == list(range(8))
    workers = {w["id"]: w for w in job.public()["workers"]}
    assert sum(w["done"] for w in workers.values()) == 8
    assert {w["state"] for w in workers.values()} == {"done"} and workers["a"]["label"] == "Máy a"
    meta = job.public()["chapters"]
    assert all(c["seconds"] > 0 and c["file"].endswith(".m4a") for c in meta)


def test_with_several_machines_the_longest_chapters_go_out_first(settings, store, make_epub):
    long_text = " ".join([FILLER] * 5)
    book = [(f"Chương {k}", [f"Mốc số {k}.", long_text if k in (4, 5) else FILLER]) for k in range(1, 7)]
    job = store.create(make_epub("long.epub", book), SPEC)
    barrier, first = threading.Barrier(2), []

    def snapshot():
        first.extend(w["chapter"] for w in store.get(job.id).public()["workers"] if w["chapter"])

    a = FakeLane("a", Rendezvous(barrier, snapshot))
    b = FakeLane("b", Rendezvous(barrier))
    Runner(settings, store, Source(a, b)).run_one(store.claim_next())
    assert store.get(job.id).state is State.DONE
    assert {"Chương 4", "Chương 5"} <= set(first)


def test_a_single_machine_still_goes_in_book_order(settings, store, make_epub):
    a = FakeLane("a")
    job, _, _ = run(settings, store, make_epub, a)
    order = [c.split()[2] for c in a._engine.calls if c.startswith("Mốc")]
    assert job.state is State.DONE and order == [str(k) for k in range(1, 7)]


def test_a_machine_that_cannot_get_ready_drops_out_and_the_others_finish(settings, store, make_epub):
    good = FakeLane("a")
    bad = FakeLane("b", prepare_error=WorkerError("cannot reach the voice worker at http://phone:8780"))
    job, md5, _ = run(settings, store, make_epub, good, bad)
    assert job.state is State.DONE and indexes(settings, md5) == list(range(6))
    workers = {w["id"]: w for w in job.public()["workers"]}
    assert workers["b"]["state"] == "failed" and "cannot reach the voice worker" in workers["b"]["error"]
    assert workers["a"]["done"] == 6 and bad._engine.calls == []


def test_a_machine_that_dies_mid_chapter_hands_the_chapter_back(settings, store, make_epub):
    class Dies(FakeEngine):
        def synthesize(self, text, dialog=False):
            if len(self.calls) >= 2:
                raise WorkerError("the voice worker said 502")
            return super().synthesize(text, dialog)

    good, dying = FakeLane("a"), FakeLane("b", Dies())
    job, md5, _ = run(settings, store, make_epub, good, dying)
    assert job.state is State.DONE and indexes(settings, md5) == list(range(6))
    assert len(Library(settings.library).entries(md5)) == 6
    workers = {w["id"]: w for w in job.public()["workers"]}
    assert workers["b"]["state"] == "failed" and workers["a"]["done"] == 6


def test_when_every_machine_fails_the_job_says_why_for_each(settings, store, make_epub):
    a = FakeLane("a", prepare_error=WorkerError("cannot reach A"))
    b = FakeLane("b", prepare_error=LaneError("no such voice on B"))
    job, md5, _ = run(settings, store, make_epub, a, b)
    assert job.state is State.ERROR and indexes(settings, md5) == []
    assert "no machine could finish" in job.error and "cannot reach A" in job.error and "no such voice on B" in job.error


def test_finished_chapters_survive_when_the_last_machine_dies_and_retry_continues(settings, store, make_epub):
    class DiesAfterOne(FakeEngine):
        def synthesize(self, text, dialog=False):
            if "Mốc số 2" in text:
                raise WorkerError("gone")
            return super().synthesize(text, dialog)

    book = make_epub("r.epub", chapters(3))
    job = store.create(book, SPEC)
    Runner(settings, store, Source(FakeLane("a", DiesAfterOne()))).run_one(store.claim_next())
    assert store.get(job.id).state is State.ERROR and indexes(settings, md5_file(book)) == [0]
    store.retry(job.id)
    again = FakeLane("a")
    Runner(settings, store, Source(again)).run_one(store.claim_next())
    assert store.get(job.id).state is State.DONE and indexes(settings, md5_file(book)) == [0, 1, 2]
    assert not any("Mốc số 1" in c for c in again._engine.calls)


def test_an_unexpected_error_stops_the_whole_job_with_its_message(settings, store, make_epub):
    barrier = threading.Barrier(2)
    a, b = FakeLane("a", Rendezvous(barrier)), FakeLane("b", Rendezvous(barrier, fail_on="Mốc"))
    job, _, _ = run(settings, store, make_epub, a, b)
    assert job.state is State.ERROR and "boom" in job.error


def test_choosing_machines_in_the_plan_uses_only_those(settings, store, make_epub):
    a, b = FakeLane("a"), FakeLane("b")
    job, md5, _ = run(settings, store, make_epub, a, b, plan=Plan(workers=("b",)))
    assert job.state is State.DONE and a.prepared == 0 and b.prepared == 1 and a._engine.calls == []
    assert [w["id"] for w in job.public()["workers"]] == ["b"]


def test_a_plan_naming_machines_that_no_longer_exist_fails_with_advice(settings, store, make_epub):
    job, _, _ = run(settings, store, make_epub, FakeLane("a"), plan=Plan(workers=("gone",)))
    assert job.state is State.ERROR and "any more" in job.error


def test_a_book_that_is_already_complete_does_not_wake_any_machine(settings, store, make_epub):
    book = make_epub("done.epub", chapters(2))
    store.create(book, SPEC)
    Runner(settings, store, Source(FakeLane("a"))).run_one(store.claim_next())
    again = FakeLane("a", prepare_error=AssertionError("must not be prepared"))
    job = store.create(book, SPEC)
    Runner(settings, store, Source(again)).run_one(store.claim_next())
    assert store.get(job.id).state is State.DONE and again.prepared == 0


def test_the_job_does_not_wait_for_a_machine_still_loading_its_model(settings, store, make_epub):
    gate = threading.Event()
    fast, slow = FakeLane("a"), FakeLane("b", gate=gate)
    started = time.monotonic()
    job, md5, _ = run(settings, store, make_epub, fast, slow)
    took = time.monotonic() - started
    gate.set()
    assert job.state is State.DONE and indexes(settings, md5) == list(range(6)) and took < 10
    assert {w["id"]: w["state"] for w in job.public()["workers"]}["b"] == "preparing"


def test_the_page_can_see_what_a_loading_machine_is_doing(settings, store, make_epub):
    gate, seen = threading.Event(), {}
    slow = FakeLane("b", gate=gate)
    job = store.create(make_epub("w.epub", chapters(2)), SPEC)
    fast = FakeLane("a", FakeEngine())
    original = fast._engine.synthesize

    def peek(text, dialog=False):
        seen.update({w["id"]: w for w in store.get(job.id).public()["workers"]})
        return original(text, dialog)

    fast._engine.synthesize = peek
    Runner(settings, store, Source(fast, slow)).run_one(store.claim_next())
    gate.set()
    assert seen["b"]["state"] == "preparing" and "loading the model" in seen["b"]["detail"]
    assert seen["a"]["state"] == "rendering" and seen["a"]["clip_count"] > 0


def test_cancelling_stops_every_lane_and_keeps_what_is_finished(settings, store, make_epub):
    book = make_epub("c.epub", chapters(8))
    job = store.create(book, SPEC)

    class Cancelling(FakeEngine):
        def synthesize(self, text, dialog=False):
            if len(self.calls) == 3:
                store.request_cancel(job.id)
            return super().synthesize(text, dialog)

    a, b = FakeLane("a", Cancelling()), FakeLane("b", FakeEngine())
    Runner(settings, store, Source(a, b)).run_one(store.claim_next())
    assert store.get(job.id).state is State.CANCELLED
    assert len(indexes(settings, md5_file(book))) < 8
    before = len(a._engine.calls) + len(b._engine.calls)
    time.sleep(0.3)
    assert len(a._engine.calls) + len(b._engine.calls) == before


def test_an_old_database_with_clip_columns_still_opens(tmp_path):
    import sqlite3

    from app.jobs import JobStore

    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL, model TEXT NOT NULL, voice TEXT, dialog_voice TEXT, temperature REAL NOT NULL,
      read_titles INTEGER NOT NULL, chapters_done INTEGER NOT NULL DEFAULT 0, chapters_total INTEGER NOT NULL DEFAULT 0,
      clip_index INTEGER NOT NULL DEFAULT 0, clip_count INTEGER NOT NULL DEFAULT 0, output TEXT, error TEXT,
      cancel INTEGER NOT NULL DEFAULT 0, plan TEXT, chapters TEXT, book_md5 TEXT, created REAL NOT NULL, updated REAL NOT NULL)""")
    con.execute("INSERT INTO jobs (id, source, state, model, temperature, read_titles, created, updated)"
                " VALUES ('x','/b.epub','done','nano',0.8,1,1,1)")
    con.commit(); con.close()
    store = JobStore(db)
    assert store.get("x").state is State.DONE and store.get("x").public()["workers"] == []
    new = store.create(tmp_path / "n.epub", SPEC, md5="a" * 32)
    assert store.get(new.id).state is State.QUEUED
