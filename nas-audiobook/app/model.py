from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


@dataclass(frozen=True)
class Clip:
    text: str
    pause_ms: int
    dialog: bool = False


@dataclass(frozen=True)
class Chapter:
    title: str
    clips: tuple[Clip, ...]
    index: int = 0
    text_hash: str = ""
    chars: int = 0


@dataclass(frozen=True)
class Cover:
    data: bytes
    ext: str


@dataclass(frozen=True)
class Book:
    title: str
    author: str
    language: str
    cover: Cover | None
    chapters: tuple[Chapter, ...]


@dataclass(frozen=True)
class Voice:
    model: str
    voice: str | None
    temperature: float
    read_titles: bool
    dialog_voice: str | None = None

    def signature(self) -> str:
        raw = f"{self.model}|{self.voice}|{self.dialog_voice}|{self.temperature}|{int(self.read_titles)}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Plan:
    """How to turn a file into chapters: which processor and settings, which chapters, and metadata overrides."""

    processor: str = "auto"
    params: tuple[tuple[str, str], ...] = ()
    selection: tuple[int, ...] | None = None
    title: str | None = None
    author: str | None = None
    overwrite: bool = False
    workers: tuple[str, ...] | None = None  # ids of the machines to use; None means every machine that is switched on

    def to_json(self) -> str:
        return json.dumps({"processor": self.processor, "params": dict(self.params), "selection": self.selection,
                           "title": self.title, "author": self.author, "overwrite": self.overwrite,
                           "workers": self.workers},
                          ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> "Plan":
        if not raw:
            return cls()
        d = json.loads(raw)
        return cls(d.get("processor") or "auto", tuple(sorted((d.get("params") or {}).items())),
                   None if d.get("selection") is None else tuple(d["selection"]), d.get("title"), d.get("author"),
                   bool(d.get("overwrite")), None if d.get("workers") is None else tuple(d["workers"]))


class State(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    ASSEMBLING = "assembling"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"
    PAUSED = "paused"  # stopped by the user, keeps its finished chapters, resumes with retry


ACTIVE = (State.QUEUED, State.RUNNING, State.ASSEMBLING)
