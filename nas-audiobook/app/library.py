"""The audio library: one folder per book, named by the MD5 of the book file, holding per-chapter .m4a files
and a book.json manifest that says which chapter text each file was made from and with which voice."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from . import params
from .model import Cover

MD5 = re.compile(r"^[0-9a-f]{32}$")
MANIFEST = "book.json"


class LibraryError(Exception):
    pass


def md5_file(path: Path) -> str:
    h = hashlib.md5()
    with path.open("rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class AudioEntry:
    file: str
    index: int
    title: str
    text_hash: str
    chars: int
    seconds: float
    bytes: int
    model: str
    voice: str | None
    dialog_voice: str | None
    params: dict[str, float | int | None]
    read_titles: bool
    voice_sig: str
    processor: str
    created: float

    def voice_label(self) -> str:
        label = f"{self.model.capitalize()} · {self.voice or 'default voice'}"
        return f"{label} / {self.dialog_voice}" if self.dialog_voice else label


def entry_from(raw: dict) -> AudioEntry:
    return AudioEntry(**{k: v for k, v in raw.items() if k not in ("temperature", "params")}, params=params.of_record(raw))


class Library:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def dir(self, md5: str) -> Path:
        if not MD5.match(md5):
            raise LibraryError("that is not a book id")
        return self.root / md5

    def _read(self, md5: str) -> dict | None:
        try:
            return json.loads((self.dir(md5) / MANIFEST).read_text(encoding="utf8"))
        except (OSError, ValueError):
            return None

    def _write(self, md5: str, data: dict) -> None:
        folder = self.dir(md5)
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / (MANIFEST + ".part")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf8")
        os.replace(tmp, folder / MANIFEST)

    def ensure_book(self, md5: str, *, title: str, author: str, language: str, source_name: str,
                    cover: Cover | None) -> None:
        with self._lock:
            data = self._read(md5) or {"md5": md5, "created": time.time(), "cover": None, "audio": []}
            data.update(title=title, author=author, language=language, source_name=source_name, updated=time.time())
            if cover:
                name = f"cover.{cover.ext}"
                folder = self.dir(md5)
                folder.mkdir(parents=True, exist_ok=True)
                if data.get("cover") not in (None, name):
                    (folder / data["cover"]).unlink(missing_ok=True)
                target = folder / name
                if not target.exists() or target.read_bytes() != cover.data:
                    target.write_bytes(cover.data)
                data["cover"] = name
            self._write(md5, data)

    def set_cover(self, md5: str, cover: Cover) -> list[AudioEntry] | None:
        """Make this the book's cover. Returns its audio entries so the caller can update their pictures, or None if
        the book is not in the library."""
        with self._lock:
            data = self._read(md5)
            if data is None:
                return None
            name = f"cover.{cover.ext}"
            folder = self.dir(md5)
            if data.get("cover") not in (None, name):
                (folder / data["cover"]).unlink(missing_ok=True)
            (folder / name).write_bytes(cover.data)
            data.update(cover=name, updated=time.time())
            self._write(md5, data)
            return self.entries(md5)

    def set_details(self, md5: str, *, title: str, author: str) -> list[AudioEntry]:
        """Rename a book. Returns the audio entries so the caller can rewrite their tags."""
        with self._lock:
            data = self._read(md5)
            if data is None:
                raise LibraryError("the book is not in the library")
            data.update(title=title, author=author, updated=time.time())
            self._write(md5, data)
            return self.entries(md5)

    def add_audio(self, md5: str, entry: AudioEntry) -> None:
        with self._lock:
            data = self._read(md5)
            if data is None:
                raise LibraryError("the book is not in the library")
            data["audio"] = [a for a in data["audio"] if a["file"] != entry.file] + [asdict(entry)]
            data["updated"] = time.time()
            self._write(md5, data)

    def entries(self, md5: str) -> list[AudioEntry]:
        """Entries whose file still exists, ordered by voice then chapter index."""
        data = self._read(md5)
        if data is None:
            return []
        folder = self.dir(md5)
        found = [entry_from(a) for a in data.get("audio", []) if (folder / a["file"]).is_file()]
        return sorted(found, key=lambda a: (a.voice_sig, a.index))

    def find(self, md5: str, text_hash: str, voice_sig: str) -> AudioEntry | None:
        return next((a for a in self.entries(md5) if a.text_hash == text_hash and a.voice_sig == voice_sig), None)

    def audio_by_hash(self, md5: str) -> dict[str, list[AudioEntry]]:
        out: dict[str, list[AudioEntry]] = {}
        for a in self.entries(md5):
            out.setdefault(a.text_hash, []).append(a)
        return out

    def manifest(self, md5: str) -> dict | None:
        data = self._read(md5)
        return None if data is None else {k: v for k, v in data.items() if k != "audio"}

    def remove_audio(self, md5: str, file: str) -> bool:
        with self._lock:
            data = self._read(md5)
            if data is None or not any(a["file"] == file for a in data["audio"]):
                return False
            (self.dir(md5) / Path(file).name).unlink(missing_ok=True)
            data["audio"] = [a for a in data["audio"] if a["file"] != file]
            data["updated"] = time.time()
            self._write(md5, data)
            return True

    def delete_book(self, md5: str) -> bool:
        with self._lock:
            folder = self.dir(md5)
            existed = folder.is_dir()
            shutil.rmtree(folder, ignore_errors=True)
            return existed

    def list_books(self) -> list[dict]:
        books = []
        for folder in self.root.iterdir():
            if not (folder.is_dir() and MD5.match(folder.name)):
                continue
            data = self._read(folder.name)
            if data is None:
                continue
            entries = self.entries(folder.name)
            voices = list(dict.fromkeys(a.voice_label() for a in entries))
            books.append({"md5": folder.name, "title": data.get("title", ""), "author": data.get("author", ""),
                          "language": data.get("language", ""), "source_name": data.get("source_name", ""),
                          "has_cover": bool(data.get("cover")),
                          "audio_count": len(entries), "seconds": round(sum(a.seconds for a in entries)),
                          "voices": voices, "updated": data.get("updated", 0)})
        return sorted(books, key=lambda b: -b["updated"])
