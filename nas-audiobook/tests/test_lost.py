"""A machine that goes out of reach never stops a book that other machines can finish, comes back into the book when
it returns, and a book with no reachable machine goes back in the queue instead of failing."""
import threading
import time

import httpx
import pytest

from app.jobs import WAITING_NOTE, Runner
from app.library import Library, md5_file
from app.model import Plan, State, Voice
from app.remote import RemoteBackend, WorkerError, WorkerOffline
from app.tts import LaneError, Unreachable
from conftest import FakeEngine
from test_balance import FakeLane, Source
from test_queue import book, wait_for

SPEC = Voice("nano", None, 0.8, True)


class Switch:
    """A machine that can be switched off and on while the test runs."""

    def __init__(self, up=True):
        self.up = threading.Event()
        if up:
            self.up.set()


class Flaky(FakeEngine):
    def __init__(self, switch, pace=0.0):
        super().__init__()
        self.switch, self.pace = switch, pace

    def synthesize(self, text, dialog=False):
        if not self.switch.up.is_set():
            raise WorkerOffline("cannot reach the voice worker (ConnectError)")
        time.sleep(self.pace)
        return super().synthesize(text, dialog)


class FlakyLane(FakeLane):
    def __init__(self, id, switch, pace=0.0):
        super().__init__(id, Flaky(switch, pace))
        self.switch = switch

    def engine(self, spec, report=lambda _m: None):
        if not self.switch.up.is_set():
            raise WorkerOffline("cannot reach the voice worker (ConnectError)")
        return super().engine(spec, report)


@pytest.fixture
def start(settings, store):
    runners = []

    def go(*lanes, retry=0.05):
        runner = Runner(settings, store, Source(*lanes), idle_seconds=0.02, retry_seconds=retry)
        runner.start()
        runners.append(runner)
        return runner

    yield go
    for r in runners:
        r.stop()


def state(store, job):
    return store.get(job.id).state


def lanes_of(store, job):
    return {w["id"]: w for w in store.get(job.id).public()["workers"]}


def test_a_machine_that_is_off_from_the_start_does_not_stop_the_book(store, make_epub, start):
    a, b = FakeLane("a"), FlakyLane("b", Switch(up=False))
    job = store.create(book(make_epub, "A", 5), SPEC)
    start(a, b, retry=60)
    assert wait_for(lambda: state(store, job) is State.DONE), store.get(job.id).error
    assert len(a._engine.calls) > 0 and lanes_of(store, job)["b"]["state"] == "failed"
    assert "cannot reach" in lanes_of(store, job)["b"]["error"]


def test_a_machine_that_goes_off_mid_chapter_hands_the_chapter_to_the_others(store, make_epub, start):
    a, switch = FakeLane("a", Flaky(Switch(), pace=0.03)), Switch()
    b = FlakyLane("b", switch, pace=0.03)
    job = store.create(book(make_epub, "A", 8), SPEC)
    start(a, b, retry=60)
    assert wait_for(lambda: len(b._engine.calls) > 0)
    switch.up.clear()
    assert wait_for(lambda: state(store, job) is State.DONE), store.get(job.id).error
    assert store.get(job.id).chapters_done == 8


def test_a_machine_that_comes_back_takes_chapters_again(store, make_epub, start):
    a, switch = FakeLane("a", Flaky(Switch(), pace=0.04)), Switch(up=False)
    b = FlakyLane("b", switch, pace=0.01)
    job = store.create(book(make_epub, "A", 16), SPEC)
    start(a, b, retry=0.05)
    assert wait_for(lambda: lanes_of(store, job).get("b", {}).get("state") == "failed")
    switch.up.set()
    assert wait_for(lambda: state(store, job) is State.DONE), store.get(job.id).error
    assert len(b._engine.calls) > 0 and lanes_of(store, job)["b"]["done"] > 0


def test_when_no_machine_can_be_reached_the_book_goes_back_in_the_queue_and_continues_later(
        settings, store, make_epub, start):
    switch = Switch(up=False)
    b = FlakyLane("b", switch)
    job = store.create(book(make_epub, "A", 3), SPEC)
    start(b, retry=0.3)
    assert wait_for(lambda: (j := store.get(job.id)).state is State.QUEUED and (j.error or "").startswith(WAITING_NOTE))
    assert "cannot reach" in store.get(job.id).error and store.get(job.id).chapters_done == 0
    switch.up.set()
    assert wait_for(lambda: state(store, job) is State.DONE), store.get(job.id).error
    assert store.get(job.id).error is None and store.get(job.id).chapters_done == 3


def test_a_book_that_lost_its_last_machine_keeps_the_chapters_it_had_finished(settings, store, make_epub, start):
    switch = Switch()
    b = FlakyLane("b", switch, pace=0.05)
    path = book(make_epub, "A", 6)
    job = store.create(path, SPEC)
    start(b, retry=0.3)
    assert wait_for(lambda: store.get(job.id).chapters_done >= 2)
    switch.up.clear()
    assert wait_for(lambda: store.get(job.id).state is State.QUEUED)
    kept = store.get(job.id).chapters_done
    assert kept >= 2 and len(Library(settings.library).entries(md5_file(path))) >= kept
    switch.up.set()
    assert wait_for(lambda: state(store, job) is State.DONE)
    assert store.get(job.id).chapters_done == 6


def test_a_requeued_book_waits_before_trying_again_and_can_be_cancelled(settings, store, make_epub, start):
    b = FlakyLane("b", Switch(up=False))
    job = store.create(book(make_epub, "A", 2), SPEC)
    start(b, retry=60)
    assert wait_for(lambda: store.get(job.id).state is State.QUEUED and store.get(job.id).error)
    prepared = b.prepared
    time.sleep(0.3)
    assert b.prepared == prepared  # nothing hammers the machine while the book waits
    store.request_cancel(job.id)
    assert state(store, job) is State.CANCELLED


def test_a_machine_that_only_cannot_do_the_job_still_fails_the_book_instead_of_waiting_forever(
        store, make_epub, start):
    class Cannot(FakeLane):
        def engine(self, spec, report=lambda _m: None):
            raise LaneError("the voice 'X' does not exist on this machine")

    job = store.create(book(make_epub, "A", 2), SPEC)
    start(Cannot("a"), retry=0.05)
    assert wait_for(lambda: state(store, job) is State.ERROR)
    assert "does not exist" in store.get(job.id).error


def test_another_book_is_not_held_up_by_a_machine_that_went_away(store, make_epub, start):
    a, b = FakeLane("a"), FlakyLane("b", Switch(up=False))
    stuck = store.create(book(make_epub, "A", 2), SPEC, Plan(workers=("b",)))
    other = store.create(book(make_epub, "B", 2), SPEC, Plan(workers=("a",)))
    start(a, b, retry=60)
    assert wait_for(lambda: state(store, other) is State.DONE)
    assert state(store, stuck) is State.QUEUED


# --- telling "out of reach" from "said no" ---------------------------------------------------

def remote(handler):
    client = httpx.Client(base_url="http://worker", transport=httpx.MockTransport(handler))
    return RemoteBackend("http://worker", None, client=client, retries=1, backoff=0, sleep=lambda _s: None)


def test_a_worker_that_cannot_be_reached_is_unreachable_and_one_that_says_no_is_not():
    def down(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(WorkerOffline) as info:
        remote(down).request("GET", "/health")
    assert isinstance(info.value, Unreachable) and "cannot reach" in str(info.value)

    with pytest.raises(WorkerError) as info:
        remote(lambda r: httpx.Response(501, json={"detail": "no"})).request("GET", "/health")
    assert not isinstance(info.value, Unreachable)

    def busy(request):
        return httpx.Response(503, json={"detail": {"loading": True, "message": "loading"}})

    with pytest.raises(WorkerError) as info:
        remote(busy).request("GET", "/voices")
    assert not isinstance(info.value, Unreachable)  # reachable, only busy


def test_a_connection_that_cannot_be_made_is_given_up_on_quickly():
    assert RemoteBackend("http://worker").client.timeout.connect == 5.0
