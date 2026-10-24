from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_data_dir(container_dir: Path = Path("/data")) -> Path:
    """/data inside the Docker images, where the volume is mounted. Anywhere else (running `python -m app` on a Mac, a
    Windows PC or a Linux machine) there is no writable /data, so the data folder is ./data where you start it."""
    return container_dir if container_dir.is_dir() and os.access(container_dir, os.W_OK) else Path("data")


def available_cpus() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def _optional_model(raw: str) -> str | None:
    raw = raw.strip().lower()
    return None if raw in ("", "none") else raw


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    model: str = "nano"
    voice: str | None = None
    dialog_voice: str | None = None
    precision: str = "fp32"
    device: str = "auto"                    # auto, cpu, cuda, directml, coreml or openvino (see accel.py)
    temperature: float = 0.8
    threads: int = 0
    backend: str = "local"
    worker_url: str | None = None
    worker_token: str | None = None
    english_model: str | None = "english"   # what an English book is read with; None keeps `model`
    english_voice: str | None = None
    read_titles: bool = True
    scan_interval: float = 10.0
    settle_seconds: float = 5.0
    port: int = 8765

    @property
    def inbox(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def books(self) -> Path:
        return self.data_dir / "books"

    @property
    def library(self) -> Path:
        return self.data_dir / "library"

    @property
    def db(self) -> Path:
        return self.data_dir / "jobs.sqlite"

    @property
    def uploads(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def samples(self) -> Path:
        return self.data_dir / "cache" / "samples"

    @property
    def m4b_cache(self) -> Path:
        return self.data_dir / "cache" / "m4b"

    @property
    def engine_config(self) -> Path:
        return self.data_dir / "config" / "engine.json"

    @property
    def calibre_config(self) -> Path:
        return self.data_dir / "config" / "calibre.json"

    @property
    def presets_config(self) -> Path:
        return self.data_dir / "config" / "presets.json"

    @property
    def voice_cache(self) -> Path:
        return self.data_dir / "cache" / "voices.json"

    @property
    def voices_dir(self) -> Path:
        return self.data_dir / "voices"

    @property
    def pronunciations(self) -> Path:
        return self.data_dir / "config" / "pronunciations.txt"

    def defaults_for(self, language: str | None) -> dict:
        """Model and voices a book in this language starts with. An English book gets the English model, whose
        voices are different ones, so the Vietnamese default voice does not carry over."""
        if (language or "").lower().startswith("en") and self.english_model and self.english_model != self.model:
            return {"model": self.english_model, "voice": self.english_voice, "dialog_voice": None}
        return {"model": self.model, "voice": self.voice, "dialog_voice": self.dialog_voice}

    def ensure_dirs(self) -> None:
        for d in (self.inbox, self.books, self.library, self.pronunciations.parent):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ
        return cls(
            data_dir=Path(e.get("DATA_DIR") or default_data_dir()).resolve(),
            model=e.get("TTS_MODEL", "nano"),
            voice=e.get("TTS_VOICE") or None,
            dialog_voice=e.get("TTS_DIALOG_VOICE") or None,
            precision=e.get("TTS_PRECISION", "fp32"),
            device=e.get("TTS_DEVICE", "auto").strip().lower() or "auto",
            temperature=float(e.get("TTS_TEMPERATURE", "0.8")),
            threads=int(e.get("TTS_THREADS", "0")) or available_cpus(),
            backend=e.get("TTS_BACKEND", "local"),
            worker_url=e.get("TTS_WORKER_URL") or None,
            worker_token=e.get("TTS_WORKER_TOKEN") or None,
            english_model=_optional_model(e.get("TTS_ENGLISH_MODEL", "english")),
            english_voice=e.get("TTS_ENGLISH_VOICE") or None,
            read_titles=e.get("READ_TITLES", "1") != "0",
            scan_interval=float(e.get("SCAN_INTERVAL", "10")),
            settle_seconds=float(e.get("SETTLE_SECONDS", "5")),
            port=int(e.get("PORT", "8765")),
        )
