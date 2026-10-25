"""Several books at once: a machine that has finished its part of one book starts on the next, books keep their order
for the machines they share, and a book never waits for a machine it did not ask for."""
import threading
import time

import pytest

from app.jobs import Runner
from app.model import Plan, State, Voice
from app.scheduler import Gates
from app.tts import LaneError
from conftest import FILLER, FakeEngine
from test_balance import FakeLane, Source

SPEC = Voice("nano", None, (), True)


def book(make_epub, tag, count):
    return make_epub(f"{tag}.epub", [(f"Chương {k}", [f"Sách {tag} mốc {k}.", FILLER]) for k in range(1, count + 1)])


def wait_for(predicate, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def runner_for(settings, store):
    started = []

    def start(*lanes):
        runner = Runner(settings, store, Source(*lanes), idle_seconds=0.02)
        runner.start()
        started.append(runner)
        return runner

    yield start
    for runner in started:
        runner.stop()


class Hold(FakeEngine):
    """Blocks on its first call until `release` is set, like a slow machine that is still working on a chapter."""

    def __init__(self, release):
        super().__init__()
        self.release, self.first = release, True

    def synthesize(self, text, dialog=False):
        if self.first:
            self.first = False
            assert self.release.wait(15), "the test never released the slow machine"
        return super().synthesize(text, dialog)


def state(store, job):
    return store.get(job.id).state


def test_a_machine_that_finished_its_share_starts_the_next_book_while_the_slow_one_finishes_the_first(
        settings, store, make_epub, runner_for):
    release = threading.Event()
    m1, rtx = FakeLane("m1", Hold(release)), FakeLane("rtx")
    first = store.create(book(make_epub, "A", 2), SPEC, Plan(workers=("m1", "rtx")))
    second = store.create(book(make_epub, "B", 5), SPEC, Plan(workers=("rtx",)))
    runner_for(m1, rtx)
    assert wait_for(lambda: state(store, second) is State.DONE), store.get(second.id).error
    done = store.get(first.id)
    assert done.state is State.RUNNING and done.chapters_done == 1  # RTX did its chapter; M1 is still on the other
    release.set()
    assert wait_for(lambda: state(store, first) is State.DONE)
    assert store.get(second.id).chapters_done == 5


def test_a_book_waits_while_a_machine_it_wants_still_has_chapters_for_an_older_book(
        settings, store, make_epub, runner_for):
    a = FakeLane("a")
    older = store.create(book(make_epub, "A", 4), SPEC)
    newer = store.create(book(make_epub, "B", 2), SPEC)
    runner_for(a)
    assert wait_for(lambda: state(store, newer) is State.DONE)
    calls = a._engine.calls
    last_older = max(i for i, t in enumerate(calls) if "Sách A" in t)
    first_newer = min(i for i, t in enumerate(calls) if "Sách B" in t)
    assert last_older < first_newer and state(store, older) is State.DONE


def test_a_queued_book_stays_queued_until_one_of_its_machines_is_free_and_others_are_not_blocked(
        settings, store, make_epub, runner_for):
    release = threading.Event()
    a, b = FakeLane("a", Hold(release)), FakeLane("b")
    busy = store.create(book(make_epub, "A", 2), SPEC, Plan(workers=("a",)))
    waits = store.create(book(make_epub, "B", 2), SPEC, Plan(workers=("a",)))
    other = store.create(book(make_epub, "C", 2), SPEC, Plan(workers=("b",)))
    runner_for(a, b)
    assert wait_for(lambda: state(store, other) is State.DONE)  # b was free, so a younger book used it at once
    assert state(store, busy) is State.RUNNING and state(store, waits) is State.QUEUED
    release.set()
    assert wait_for(lambda: state(store, waits) is State.DONE and state(store, busy) is State.DONE)


def test_a_chapter_handed_back_by_a_failed_machine_goes_to_the_older_book_between_two_chapters_of_a_newer_one(
        settings, store, make_epub, runner_for):
    b_fails = threading.Event()
    newer_started = threading.Event()

    class Slow(FakeEngine):
        def synthesize(self, text, dialog=False):
            if "Sách B" in text:
                newer_started.set()
                time.sleep(0.05)
            return super().synthesize(text, dialog)

    class Dying(FakeEngine):
        def synthesize(self, text, dialog=False):
            assert b_fails.wait(15)
            raise LaneError("the machine went away")

    a, b = FakeLane("a", Slow()), FakeLane("b", Dying())
    older = store.create(book(make_epub, "A", 2), SPEC, Plan(workers=("a", "b")))
    newer = store.create(book(make_epub, "B", 16), SPEC, Plan(workers=("a",)))
    runner_for(a, b)
    assert newer_started.wait(10)  # a finished its chapter of the older book and moved on to the newer one
    b_fails.set()
    assert wait_for(lambda: state(store, older) is State.DONE), store.get(older.id).error
    assert store.get(newer.id).chapters_done < 16  # the older book did not wait for the newer one to finish
    assert wait_for(lambda: state(store, newer) is State.DONE)


def test_cancelling_a_book_that_is_waiting_for_a_machine_stops_it(settings, store, make_epub, runner_for):
    release = threading.Event()
    a = FakeLane("a", Hold(release))
    busy = store.create(book(make_epub, "A", 1), SPEC, Plan(workers=("a",)))
    waiting = store.create(book(make_epub, "B", 1), SPEC, Plan(workers=("a", "b")))
    b = FakeLane("b", prepare_error=LaneError("offline"))  # b was free, so the book started; only a is left to wait for
    runner_for(a, b)
    assert wait_for(lambda: state(store, waiting) is State.RUNNING)
    store.request_cancel(waiting.id)
    assert wait_for(lambda: state(store, waiting) is State.CANCELLED)
    release.set()
    assert wait_for(lambda: state(store, busy) is State.DONE)


def test_a_machine_in_use_says_so_to_the_book_waiting_for_it(settings, store, make_epub, runner_for):
    release = threading.Event()
    a, b = FakeLane("a", Hold(release)), FakeLane("b")
    store.create(book(make_epub, "A", 2), SPEC, Plan(workers=("a",)))
    waits = store.create(book(make_epub, "B", 2), SPEC, Plan(workers=("a", "b")))
    runner_for(a, b)
    assert wait_for(lambda: any(w["id"] == "a" and w["state"] == "waiting" and "another book" in w["detail"]
                                for w in store.get(waits.id).public()["workers"]))
    release.set()
    assert wait_for(lambda: state(store, waits) is State.DONE)


def test_gates_hand_a_machine_to_the_oldest_waiter_and_giving_a_ticket_back_twice_is_harmless():
    gate = Gates().gate("m")
    old, new = gate.queue((1.0, "a")), gate.queue((2.0, "b"))
    assert gate.free is False and gate.contended((2.0, "b")) is True and gate.contended((1.0, "a")) is False
    got = []
    t = threading.Thread(target=lambda: got.append(new.wait()))
    t.start()
    time.sleep(0.1)
    assert got == []  # the older ticket goes first even though the newer one is waiting
    assert old.wait() is True
    old.release(); old.release()
    t.join(5)
    assert got == [True]
    new.release()
    assert gate.free is True


def test_a_ticket_stops_waiting_when_asked_to():
    gate = Gates().gate("m")
    holder = gate.queue((1.0, "a"))
    assert holder.wait()
    waiter = gate.queue((2.0, "b"))
    assert waiter.wait(should_stop=lambda: True, poll=0.01) is False
    assert gate.contended((3.0, "c")) is False  # a ticket that gave up left the line


def test_a_machine_that_got_ready_but_found_no_chapter_left_shows_as_idle_not_preparing(
        settings, store, make_epub, runner_for):
    release = threading.Event()
    a, b = FakeLane("a", Hold(release)), FakeLane("b", Hold(release))
    job = store.create(book(make_epub, "A", 1), SPEC)
    runner_for(a, b)
    assert wait_for(lambda: sorted(w["state"] for w in store.get(job.id).public()["workers"]) == ["idle", "rendering"])
    release.set()
    assert wait_for(lambda: state(store, job) is State.DONE)
