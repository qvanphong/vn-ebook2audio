from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from .config import Settings
from .books import load_source
from .ingest import SUPPORTED
from .jobs import JobStore
from .model import Voice
from .sources import adopt

log = logging.getLogger("watcher")


def default_voice(settings: Settings, language: str | None = None) -> Voice:
    d = settings.defaults_for(language)
    return Voice(d["model"], d["voice"], settings.temperature, settings.read_titles, d["dialog_voice"])


def book_language(path: Path) -> str | None:
    """The language a file is in, or None if it cannot be read (the job will then report the real problem)."""
    try:
        return load_source(path).language
    except Exception:
        return None


def scan_inbox(settings: Settings, store: JobStore, now: float | None = None) -> int:
    """Queue every settled, supported file in the inbox and archive the source. Returns files queued."""
    now = time.time() if now is None else now
    queued = 0
    for path in sorted(settings.inbox.rglob("*")):
        if not path.is_file() or path.name.startswith(".") or path.suffix.lower() not in SUPPORTED:
            continue
        if now - path.stat().st_mtime < settings.settle_seconds:
            continue
        md5, target = adopt(settings.books, path)
        store.create(target, default_voice(settings, book_language(target)), md5=md5, title=path.stem)
        log.info("queued %s (book %s)", path.name, md5)
        queued += 1
    return queued


class Watcher(threading.Thread):
    def __init__(self, settings: Settings, store: JobStore):
        super().__init__(name="watcher", daemon=True)
        self.settings, self.store = settings, store
        self._halt = threading.Event()

    def stop(self) -> None:
        self._halt.set()

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                scan_inbox(self.settings, self.store)
            except Exception:
                log.exception("inbox scan failed")
            self._halt.wait(self.settings.scan_interval)
