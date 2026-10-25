"""Persistent job queue and the chapter-by-chapter render loop with resume."""
from __future__ import annotations

import collections
import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Protocol, Sequence

import numpy as np

from . import ingest, m4b, params
from .config import Settings
from .library import AudioEntry, Library, md5_file
from .model import ACTIVE, Book, Chapter, Clip, Plan, State, Voice
from .scheduler import Gates
from .text import TextOptions, load_pronunciations
from .tts import Engine, EngineFactory, LaneError, Unreachable

log = logging.getLogger("jobs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
  model TEXT NOT NULL, voice TEXT, dialog_voice TEXT, params TEXT, read_titles INTEGER NOT NULL,
  chapters_done INTEGER NOT NULL DEFAULT 0, chapters_total INTEGER NOT NULL DEFAULT 0,
  output TEXT, error TEXT, cancel INTEGER NOT NULL DEFAULT 0, pause INTEGER NOT NULL DEFAULT 0, plan TEXT, chapters TEXT,
  workers TEXT, book_md5 TEXT, created REAL NOT NULL, updated REAL NOT NULL
)
"""
CLEARABLE = (State.DONE, State.ERROR, State.CANCELLED, State.PAUSED)  # finished or stopped: safe to remove from the list


@dataclass(frozen=True)
class Job:
    id: str
    source: str
    title: str
    state: State
    model: str
    voice: str | None
    dialog_voice: str | None
    params_json: str | None
    read_titles: bool
    chapters_done: int
    chapters_total: int
    output: str | None
    error: str | None
    cancel: bool
    pause: bool
    plan_json: str | None
    chapters_json: str | None
    workers_json: str | None
    book_md5: str | None
    created: float
    updated: float

    @property
    def spec(self) -> Voice:
        return Voice(self.model, self.voice, params.stored(self.model, json.loads(self.params_json or "{}")),
                     self.read_titles, self.dialog_voice)

    @property
    def plan(self) -> Plan:
        return Plan.from_json(self.plan_json)

    def public(self) -> dict:
        hidden = ("cancel", "pause", "plan_json", "chapters_json", "workers_json", "params_json")
        d = {k: (v.value if isinstance(v, State) else v) for k, v in self.__dict__.items() if k not in hidden}
        d["params"] = dict(self.spec.params)
        d["chapters"] = json.loads(self.chapters_json) if self.chapters_json else []
        d["workers"] = json.loads(self.workers_json) if self.workers_json else []
        plan = self.plan
        d["processor"] = plan.processor
        d["selected"] = None if plan.selection is None else len(plan.selection)
        return d


class JobStore:
    def __init__(self, path: Path):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(SCHEMA)
            columns = {r["name"] for r in self._db.execute("PRAGMA table_info(jobs)")}
            for column in ("dialog_voice", "plan", "chapters", "workers", "book_md5", "params"):
                if column not in columns:
                    self._db.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
            if "temperature" in columns:  # before models had settings of their own, only Turbo used it
                for r in self._db.execute("SELECT id, model, temperature FROM jobs WHERE params IS NULL").fetchall():
                    kept = {"temperature": r["temperature"]} if r["model"] == "turbo" else {}
                    self._db.execute("UPDATE jobs SET params=? WHERE id=?", (json.dumps(kept), r["id"]))
                self._db.execute("ALTER TABLE jobs DROP COLUMN temperature")
            if "pause" not in columns:
                self._db.execute("ALTER TABLE jobs ADD COLUMN pause INTEGER NOT NULL DEFAULT 0")
            self._db.commit()

    def _row(self, r: sqlite3.Row) -> Job:
        d = dict(r)
        d["state"] = State(d["state"])
        d["read_titles"] = bool(d["read_titles"])
        d["cancel"] = bool(d["cancel"])
        d["pause"] = bool(d["pause"])
        d["plan_json"] = d.pop("plan")
        d["params_json"] = d.pop("params")
        d["chapters_json"] = d.pop("chapters")
        d["workers_json"] = d.pop("workers")
        return Job(**{f.name: d[f.name] for f in fields(Job)})  # old databases still have clip_index and clip_count

    def create(self, source: Path, spec: Voice, plan: Plan | None = None, md5: str | None = None,
               title: str | None = None) -> Job:
        plan = plan or Plan()
        md5 = md5 or md5_file(source)
        now = time.time()
        job_id = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute(
                "INSERT INTO jobs (id, source, title, state, model, voice, dialog_voice, params, read_titles, plan,"
                " book_md5, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (job_id, str(source), plan.title or title or source.stem, State.QUEUED, spec.model, spec.voice, spec.dialog_voice,
                 json.dumps(dict(spec.params)), int(spec.read_titles), plan.to_json(), md5, now, now),
            )
            self._db.commit()
        return self.get(job_id)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            r = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row(r) if r else None

    def list(self) -> list[Job]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs ORDER BY created DESC").fetchall()
        return [self._row(r) for r in rows]

    def update(self, job_id: str, **fields) -> None:
        fields["updated"] = time.time()
        if "state" in fields:
            fields["state"] = State(fields["state"]).value
        cols = ", ".join(f"{k}=?" for k in fields)
        with self._lock:
            self._db.execute(f"UPDATE jobs SET {cols} WHERE id=?", (*fields.values(), job_id))
            self._db.commit()

    def claim_next(self, accept=None) -> Job | None:
        """Start the oldest queued job, or the oldest one `accept(job)` says can start now."""
        with self._lock:
            rows = self._db.execute("SELECT * FROM jobs WHERE state=? ORDER BY created", (State.QUEUED.value,)).fetchall()
            r = next((r for r in rows if accept is None or accept(self._row(r))), None)
            if r is None:
                return None
            self._db.execute("UPDATE jobs SET state=?, updated=? WHERE id=?",
                             (State.RUNNING.value, time.time(), r["id"]))
            self._db.commit()
        return self.get(r["id"])

    def requeue_interrupted(self) -> int:
        with self._lock:
            cur = self._db.execute(
                "UPDATE jobs SET state=?, updated=? WHERE state IN (?,?)",
                (State.QUEUED.value, time.time(), State.RUNNING.value, State.ASSEMBLING.value),
            )
            self._db.commit()
            return cur.rowcount

    def request_cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None or job.state not in ACTIVE:
            return job
        if job.state is State.QUEUED:
            self.update(job_id, state=State.CANCELLED)
        else:
            self.update(job_id, cancel=1)
        return self.get(job_id)

    def request_pause(self, job_id: str) -> Job | None:
        """Stop a job but keep its finished chapters. A queued job waits as paused; a running one stops after the
        sentence in progress, the same way a cancel does, and the runner records it as paused."""
        job = self.get(job_id)
        if job is None or job.state not in ACTIVE:
            return job
        if job.state is State.QUEUED:
            self.update(job_id, state=State.PAUSED)
        else:
            self.update(job_id, pause=1, cancel=1)
        return self.get(job_id)

    def retry(self, job_id: str) -> Job | None:
        """Queue a stopped job again. Its finished chapters are reused, so this is also how a paused job resumes."""
        job = self.get(job_id)
        if job is None or job.state not in (State.ERROR, State.CANCELLED, State.PAUSED):
            return job
        self.update(job_id, state=State.QUEUED, cancel=0, pause=0, error=None)
        return self.get(job_id)

    def delete(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM jobs WHERE id=?", (job_id,))
            self._db.commit()

    def clear(self, states: Sequence[State]) -> int:
        """Remove every job in one of these states from the list (audio is kept). Returns how many were removed."""
        wanted = [State(s).value for s in states if State(s) in CLEARABLE]
        if not wanted:
            return 0
        marks = ",".join("?" * len(wanted))
        with self._lock:
            cur = self._db.execute(f"DELETE FROM jobs WHERE state IN ({marks})", wanted)
            self._db.commit()
            return cur.rowcount


class Cancelled(Exception):
    pass


class NoWorkers(RuntimeError):
    """Every machine that was asked to help failed before the book was finished."""


class MachinesLost(NoWorkers):
    """Every machine that was working on the book went out of reach. The book is not wrong, so it goes back in the
    queue and continues when a machine can be reached again."""


WAITING_NOTE = "waiting for a machine: "
RETRY_SECONDS = 20.0  # how long before a machine that was out of reach is tried again


class Lane(Protocol):
    """One machine doing part of a job. engine() gets it ready (blocking while a model loads) and returns the
    engine that speaks the job's voice."""

    id: str
    label: str

    def engine(self, spec: Voice, report=...) -> Engine: ...


class LaneSource(Protocol):
    def lanes(self, selected: Sequence[str] | None) -> list[Lane]: ...


class FactoryLane:
    """A single machine described by an engine factory: how tests and simple setups run a job."""

    id, label = "local", "This server"

    def __init__(self, factory: EngineFactory):
        self._factory = factory

    def engine(self, spec: Voice, report=lambda _message: None) -> Engine:
        return self._factory(spec)

    def lanes(self, _selected) -> list[Lane]:
        return [self]


def clip_batches(clips: Sequence[Clip], size: int) -> list[list[tuple[int, Clip]]]:
    """Consecutive clips in groups of at most `size` that share a voice (narrator or dialogue), in order, each with its
    index in the chapter. A group is one request to the machine."""
    groups: list[list[tuple[int, Clip]]] = []
    for i, clip in enumerate(clips):
        last = groups[-1] if groups else None
        if last is not None and len(last) < size and last[0][1].dialog == clip.dialog:
            last.append((i, clip))
        else:
            groups.append([(i, clip)])
    return groups


def render_chapter(chapter: Chapter, engine: Engine, sample_rate: int, check, on_clip) -> list[np.ndarray]:
    """The chapter's audio as the clips and pauses in order. They are not joined into one array: an hour of Turbo audio
    is about 700 MB, and joining and converting it needed three times that at once. With `engine.batch` above one,
    neighbouring clips in the same voice go to the machine together."""
    parts: list[np.ndarray] = []
    total = len(chapter.clips)
    for group in clip_batches(chapter.clips, engine.batch):
        check()
        on_clip(group[0][0], total)
        clips = [clip for _i, clip in group]
        if len(clips) == 1:
            audios = [engine.synthesize(clips[0].text, clips[0].dialog)]
        else:
            audios = engine.synthesize_many([clip.text for clip in clips], clips[0].dialog)
        for clip, audio in zip(clips, audios):
            parts.append(audio)
            parts.append(np.zeros(int(sample_rate * clip.pause_ms / 1000), dtype=np.float32))
    return parts


def chapter_filename(chapter: Chapter, voice_sig: str) -> str:
    return f"{chapter.index + 1:04d}-{chapter.text_hash[:8]}-{voice_sig}.m4a"


def resumed_meta(job: Job, book: Book) -> list[dict]:
    """Per-chapter progress. A retried job keeps what it already finished, so an overwrite job does not redo it."""
    fresh = [{"title": c.title, "seconds": None, "file": None} for c in book.chapters]
    try:
        old = json.loads(job.chapters_json or "[]")
    except ValueError:
        return fresh
    return old if [o.get("title") for o in old] == [f["title"] for f in fresh] else fresh


class Progress:
    """What each machine is doing and which chapters are finished, written to the job row as it changes."""

    def __init__(self, store: JobStore, job_id: str, meta: list[dict], lanes: Sequence[Lane]):
        self._store, self._job_id, self.meta = store, job_id, meta
        self._lock = threading.Lock()
        self._lanes = {l.id: {"id": l.id, "label": l.label, "state": "waiting", "detail": "", "chapter": None,
                              "clip_index": 0, "clip_count": 0, "done": 0, "error": None} for l in lanes}
        self.write()

    def lane(self, lane_id: str, **fields) -> None:
        with self._lock:
            self._lanes[lane_id].update(fields)
            self._write_locked()

    def state(self, lane_id: str) -> str:
        return self._lanes[lane_id]["state"]

    def chapter_done(self, lane_id: str, index: int, seconds: float, name: str) -> None:
        with self._lock:
            self.meta[index].update(seconds=round(seconds, 3), file=name)
            lane = self._lanes[lane_id]
            lane.update(done=lane["done"] + 1, chapter=None, clip_index=0, clip_count=0, state="idle", detail="")
            self._write_locked()

    def write(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        self._store.update(self._job_id, chapters_done=sum(m["seconds"] is not None for m in self.meta),
                           chapters=json.dumps(self.meta, ensure_ascii=False),
                           workers=json.dumps(list(self._lanes.values()), ensure_ascii=False))


class Conversion:
    """Renders the chapters that still need audio, spread over the lanes. Each lane takes the next chapter as soon
    as it is free, so a faster machine simply does more of the book. A lane that fails (offline, missing voice)
    hands its chapter back and drops out; the others carry on. Other books may be converting at the same time: a lane
    only works while it holds its machine (see scheduler.py), and gives the machine up when this book has no chapter
    left for it, so a machine that finished its share starts on the next book while slower ones finish this one."""

    def __init__(self, job: Job, book: Book, folder: Path, pending: list[int], lanes: Sequence[Lane], store: JobStore,
                 library: Library, progress: Progress, names: dict[int, str], gates: Gates | None = None,
                 started: threading.Event | None = None, retry_seconds: float = RETRY_SECONDS):
        self.job, self.book, self.folder, self.store, self.library = job, book, folder, store, library
        self.gates, self.started, self.retry_seconds = gates or Gates(), started, retry_seconds
        self.priority = (job.created, job.id)
        self.spec, self.plan = job.spec, job.plan
        self.sig = self.spec.signature()[:6]
        self.lanes, self.progress, self.names = list(lanes), progress, names
        # With several machines, longest chapters first keeps the last chapter from being a long one on a slow machine.
        order = sorted(pending, key=lambda i: -book.chapters[i].chars) if len(self.lanes) > 1 else pending
        self._todo = collections.deque(order)
        self._active = 0
        self._cv = threading.Condition()
        self.stop = threading.Event()
        self.cancelled = False
        self.fatal: BaseException | None = None
        self.failures: dict[str, str] = {}
        self.lost: set[str] = set()  # machines out of reach right now; they are tried again while the book goes on
        self.closing = threading.Event()

    def run(self) -> None:
        # Take a place in line for every machine before any lane starts, so a book queued later cannot slip in front.
        tickets = {lane.id: self.gates.gate(lane.id).queue(self.priority) for lane in self.lanes}
        if self.started is not None:
            self.started.set()
        threads = [threading.Thread(target=self._lane, args=(lane, tickets[lane.id]), name=f"lane-{lane.id}", daemon=True)
                   for lane in self.lanes]
        for t in threads:
            t.start()
        try:
            while not self.stop.is_set() and not self._drained() and any(
                    t.is_alive() and lane.id not in self.lost for t, lane in zip(threads, self.lanes)):
                with self._cv:
                    self._cv.wait(0.1)
        finally:
            self.closing.set()
            with self._cv:
                self._cv.notify_all()
            for t, lane in zip(threads, self.lanes):  # a machine still downloading its model is left to finish alone
                if self.progress.state(lane.id) != "preparing":
                    t.join(60)
            self.stop.set()
        if self.fatal is not None:
            raise self.fatal
        if self.cancelled:
            raise Cancelled()
        if self._todo or self._active:
            detail = "; ".join(f"{l.label}: {self.failures.get(l.id, 'stopped')}" for l in self.lanes)
            if self.lost:
                raise MachinesLost(f"no machine could be reached to finish the book ({detail})")
            raise NoWorkers(f"no machine could finish the book ({detail})")

    def _drained(self) -> bool:
        with self._cv:
            return not self._todo and self._active == 0

    def _work_to_do(self) -> bool:
        """Wait until a chapter is waiting for a machine (True), or the book is finished or stopped (False). A busy
        lane may still fail and hand its chapter back, so an idle lane keeps watching until everything is done."""
        with self._cv:
            while not self.stop.is_set():
                if self._todo:
                    return True
                if self._active == 0:
                    return False
                self._cv.wait(0.2)
            return False

    def _take_now(self) -> int | None:
        with self._cv:
            if self.stop.is_set() or not self._todo:
                return None
            self._active += 1
            return self._todo.popleft()

    def _release(self, index: int | None = None) -> None:
        with self._cv:
            self._active -= 1
            if index is not None:
                self._todo.appendleft(index)
            self._cv.notify_all()

    def _check(self) -> None:
        if self.stop.is_set():
            raise Cancelled()
        current = self.store.get(self.job.id)
        if current is None or current.cancel:
            self.cancelled = True
            self.stop.set()
            raise Cancelled()

    def _stopped(self) -> bool:
        """True once the job was stopped or cancelled; used while a lane waits for its machine."""
        if self.stop.is_set():
            return True
        current = self.store.get(self.job.id)
        if current is None or current.cancel:
            self.cancelled = True
            self.stop.set()
            return True
        return False

    def _lane_failed(self, lane: Lane, exc: BaseException) -> bool:
        """Record that a machine dropped out. Returns True when it may come back (it was only out of reach), in which
        case the lane waits and tries again while the book goes on."""
        message = f"{type(exc).__name__}: {exc}" if not isinstance(exc, LaneError) else str(exc)
        self.failures[lane.id] = message
        again = isinstance(exc, Unreachable)
        if again:
            self.lost.add(lane.id)
        log.warning("job %s: %s dropped out: %s", self.job.id, lane.label, message)
        self.progress.lane(lane.id, state="failed", detail=f"will try again in {self.retry_seconds:.0f} s" if again else "",
                           error=message, chapter=None, clip_index=0, clip_count=0)
        return again

    def _wait_to_retry(self) -> bool:
        """Pause before trying a lost machine again. False if the book finished or stopped meanwhile."""
        return not self.closing.wait(self.retry_seconds) and not self.stop.is_set()

    def _recovered(self, lane: Lane) -> None:
        if lane.id in self.lost:
            log.info("job %s: %s is back", self.job.id, lane.label)
        self.lost.discard(lane.id)
        self.failures.pop(lane.id, None)

    def _lane(self, lane: Lane, ticket) -> None:
        gate, engine = self.gates.gate(lane.id), None
        try:
            while ticket is not None or self._work_to_do():
                ticket = ticket or gate.queue(self.priority)
                if not gate.free:
                    self.progress.lane(lane.id, state="waiting", detail="waiting for another book to finish with this machine")
                if not ticket.wait(self._stopped):
                    return
                out_of_reach = False
                try:
                    if engine is None:
                        try:
                            self.progress.lane(lane.id, state="preparing", detail="connecting")
                            engine = lane.engine(self.spec, lambda message: self.progress.lane(
                                lane.id, state="preparing", detail=message))
                        except Exception as exc:
                            if self.stop.is_set() or not self._lane_failed(lane, exc):
                                return
                            out_of_reach = True
                        else:
                            self._recovered(lane)
                    while not out_of_reach and (index := self._take_now()) is not None:
                        try:
                            self._render(lane, engine, index)
                        except Cancelled:
                            self._release()
                            return
                        except LaneError as exc:
                            self._release(index)  # the chapter goes back in the queue for the other machines
                            if not self._lane_failed(lane, exc):
                                return
                            engine, out_of_reach = None, True
                            break
                        except BaseException as exc:
                            self.fatal = self.fatal or exc
                            self.stop.set()
                            self._release()
                            return
                        self._release()
                        if gate.contended(self.priority):  # an older book wants this machine: let it in between chapters
                            break
                    if not out_of_reach and not self.stop.is_set():
                        self.progress.lane(lane.id, state="idle", detail="")
                finally:
                    ticket.release()
                    ticket = None
                if out_of_reach and not self._wait_to_retry():
                    return
            if not self.stop.is_set() and lane.id not in self.lost:
                self.progress.lane(lane.id, state="done", detail="")
        finally:
            if ticket is not None:
                ticket.release()

    def _render(self, lane: Lane, engine: Engine, index: int) -> None:
        chapter = self.book.chapters[index]
        log.info("job %s chapter %d/%d rendering on %s", self.job.id, index + 1, len(self.book.chapters), lane.label)
        self.progress.lane(lane.id, state="rendering", detail="", chapter=chapter.title, clip_index=0,
                           clip_count=len(chapter.clips))
        audio = render_chapter(chapter, engine, engine.sample_rate, self._check,
                               lambda i, n: self.progress.lane(lane.id, clip_index=i, clip_count=n))
        seconds = sum(len(part) for part in audio) / engine.sample_rate
        name = self.names[index]
        book, spec = self.book, self.spec
        tags = {"title": chapter.title, "artist": book.author, "album_artist": book.author, "album": book.title,
                "track": str(chapter.index + 1), "genre": "Audiobook", "language": book.language}
        m4b.encode_chapter(audio, engine.sample_rate, self.folder / name, tags, book.cover)
        self.library.add_audio(self.job.book_md5, AudioEntry(
            file=name, index=chapter.index, title=chapter.title, text_hash=chapter.text_hash, chars=chapter.chars,
            seconds=round(seconds, 3), bytes=(self.folder / name).stat().st_size, model=spec.model, voice=spec.voice,
            dialog_voice=spec.dialog_voice, params=dict(spec.params), read_titles=spec.read_titles,
            voice_sig=self.sig, processor=self.plan.processor, created=time.time()))
        self.progress.chapter_done(lane.id, index, seconds, name)


def process(job: Job, settings: Settings, store: JobStore, source: LaneSource, library: Library,
            gates: Gates | None = None, started: threading.Event | None = None,
            retry_seconds: float = RETRY_SECONDS) -> Path:
    """Convert one book. `started` is set once the job has its place in line for every machine it will use (or needs
    none), so the runner can tell when the next book may be looked at."""
    try:
        return _process(job, settings, store, source, library, gates, started, retry_seconds)
    finally:
        if started is not None:
            started.set()


def _process(job: Job, settings: Settings, store: JobStore, source: LaneSource, library: Library,
             gates: Gates | None, started: threading.Event | None, retry_seconds: float) -> Path:
    opts = TextOptions(job.read_titles, bool(job.dialog_voice), load_pronunciations(settings.pronunciations))
    plan = job.plan
    book = ingest.build_book(ingest.parse(Path(job.source), plan.processor, dict(plan.params)), plan, opts)
    sig = job.spec.signature()[:6]
    md5 = job.book_md5
    library.ensure_book(md5, title=book.title, author=book.author, language=book.language,
                        source_name=Path(job.source).name, cover=book.cover)
    folder = library.dir(md5)
    store.update(job.id, title=book.title, chapters_total=len(book.chapters), output=str(folder))
    clips = [c for ch in book.chapters for c in ch.clips]
    log.info("job %s: %d chapters, %d clips (%d dialogue)", job.id, len(book.chapters), len(clips), sum(c.dialog for c in clips))

    meta = resumed_meta(job, book)
    pending: list[int] = []
    names: dict[int, str] = {}
    for i, chapter in enumerate(book.chapters):
        existing = library.find(md5, chapter.text_hash, sig)
        names[i] = existing.file if existing else chapter_filename(chapter, sig)
        finished_here = meta[i]["seconds"] is not None and (folder / names[i]).is_file()
        if finished_here or (existing and not plan.overwrite):
            meta[i].update(seconds=round(meta[i]["seconds"] if finished_here else existing.seconds, 3), file=names[i])
            log.info("job %s chapter %d/%d reused", job.id, i + 1, len(book.chapters))
        else:
            meta[i].update(seconds=None, file=None)
            pending.append(i)
    lanes = source.lanes(plan.workers) if pending else []
    progress = Progress(store, job.id, meta, lanes)
    if pending:
        if not lanes:
            raise NoWorkers("none of the machines chosen for this book exist any more; choose again in Convert or Settings")
        Conversion(job, book, folder, pending, lanes, store, library, progress, names, gates, started, retry_seconds).run()
    return folder


class Runner(threading.Thread):
    """Starts queued jobs, oldest first, as soon as a machine they can use is free. Several books can be converting at
    once, each on its own machines; a machine that has finished its part of one book moves on to the next."""

    def __init__(self, settings: Settings, store: JobStore, source: LaneSource | EngineFactory,
                 library: Library | None = None, idle_seconds: float = 1.0, retry_seconds: float = RETRY_SECONDS):
        super().__init__(name="runner", daemon=True)
        self.settings, self.store, self.idle, self.retry_seconds = settings, store, idle_seconds, retry_seconds
        self.source = source if hasattr(source, "lanes") else FactoryLane(source)
        self.library = library or Library(settings.library)
        self.gates = Gates()
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def can_start(self, job: Job) -> bool:
        """A job can start when it needs no machine, or at least one machine it may use is free. A job that went back
        in the queue because its machines were out of reach waits a little before it tries them again."""
        if (job.error or "").startswith(WAITING_NOTE) and time.time() - job.updated < self.retry_seconds:
            return False
        lanes = self.source.lanes(job.plan.workers)
        return not lanes or any(self.gates.gate(lane.id).free for lane in lanes)

    def run(self) -> None:
        self.store.requeue_interrupted()
        while not self._halt.is_set():
            job = self.store.claim_next(self.can_start)
            if job is None:
                self._halt.wait(self.idle)
                continue
            started = threading.Event()
            threading.Thread(target=self.run_one, args=(job, started), name=f"job-{job.id}", daemon=True).start()
            started.wait(60)  # until the job is in line for its machines, so the next one cannot jump ahead

    def run_one(self, job: Job, started: threading.Event | None = None) -> None:
        try:
            self.store.update(job.id, error=None)
            out = process(job, self.settings, self.store, self.source, self.library, self.gates, started,
                          self.retry_seconds)
            self.store.update(job.id, state=State.DONE, output=str(out), error=None, cancel=0, pause=0)
            log.info("job %s done: %s", job.id, out)
        except MachinesLost as exc:
            log.warning("job %s goes back in the queue: %s", job.id, exc)
            self.store.update(job.id, state=State.QUEUED, error=WAITING_NOTE + str(exc))
        except Cancelled:
            paused = (self.store.get(job.id) or job).pause
            self.store.update(job.id, state=State.PAUSED if paused else State.CANCELLED, cancel=0, pause=0)
        except (ingest.IngestError, ingest.UnsupportedFormat, ingest.ParamError) as exc:
            log.warning("job %s rejected: %s", job.id, exc)
            self.store.update(job.id, state=State.ERROR, error=f"{type(exc).__name__}: {exc}")
        except Exception as exc:
            log.exception("job %s failed", job.id)
            self.store.update(job.id, state=State.ERROR, error=f"{type(exc).__name__}: {exc}")
