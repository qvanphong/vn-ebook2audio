"""Files waiting for the user to choose how to split and voice them, kept apart from queued jobs."""
from __future__ import annotations

import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .m4b import safe_name

MAX_AGE_SECONDS = 24 * 3600


@dataclass(frozen=True)
class Upload:
    id: str
    name: str
    path: Path

    @property
    def cache_dir(self) -> Path:
        return self.path.parent


class UploadStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def create(self, filename: str) -> Upload:
        upload_id = uuid.uuid4().hex[:12]
        folder = self.dir / upload_id
        folder.mkdir()
        name = safe_name(Path(filename).stem, "book") + Path(filename).suffix.lower()
        return Upload(upload_id, name, folder / name)

    def get(self, upload_id: str) -> Upload | None:
        folder = self.dir / Path(upload_id).name
        if not folder.is_dir():
            return None
        files = [f for f in folder.iterdir() if f.is_file() and ".converted." not in f.name]
        return Upload(folder.name, files[0].name, files[0]) if files else None

    def delete(self, upload_id: str) -> None:
        shutil.rmtree(self.dir / Path(upload_id).name, ignore_errors=True)

    def sweep(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        removed = 0
        for folder in self.dir.iterdir():
            if folder.is_dir() and now - folder.stat().st_mtime > MAX_AGE_SECONDS:
                shutil.rmtree(folder, ignore_errors=True)
                removed += 1
        return removed
