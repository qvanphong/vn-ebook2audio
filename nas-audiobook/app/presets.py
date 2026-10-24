"""Voice presets: a name for an engine (model), narrator and dialogue voice and the voice settings, saved on this server
so Convert can pick one with a click. Kept in data/config/presets.json, like the machine list and Calibre-Web."""
from __future__ import annotations

import json
import os
import threading
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

NAME_MAX = 60


class PresetError(ValueError):
    pass


class PresetMissing(PresetError):
    pass


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    model: str
    voice: str | None
    dialog_voice: str | None
    temperature: float
    read_titles: bool


class PresetStore:
    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def _read(self) -> list[Preset]:
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return []
        return [Preset(**p) for p in data]

    def _write(self, presets: list[Preset]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(json.dumps([asdict(p) for p in presets], ensure_ascii=False, indent=1), encoding="utf8")
        os.replace(tmp, self.path)

    def list(self) -> list[Preset]:
        with self._lock:
            return self._read()

    def save(self, *, name: str, model: str, voice: str | None, dialog_voice: str | None, temperature: float,
             read_titles: bool, id: str | None = None) -> Preset:
        """Update the preset with this id, or the one with this name (names do not depend on capitals), or create it."""
        name = " ".join(name.split())
        if not name or len(name) > NAME_MAX:
            raise PresetError(f"a preset needs a name of 1 to {NAME_MAX} characters")
        with self._lock:
            presets = self._read()
            if id is not None:
                target = next((p for p in presets if p.id == id), None)
                if target is None:
                    raise PresetMissing("that preset no longer exists")
            else:
                target = next((p for p in presets if p.name.casefold() == name.casefold()), None)
            if any(p.name.casefold() == name.casefold() and p is not target for p in presets):
                raise PresetError(f"there is already a preset called {name!r}")
            saved = Preset(target.id if target else uuid.uuid4().hex[:8], name, model, voice or None, dialog_voice or None,
                           temperature, read_titles)
            presets = [saved if p is target else p for p in presets] if target else [*presets, saved]
            self._write(presets)
            return saved

    def delete(self, preset_id: str) -> bool:
        with self._lock:
            presets = self._read()
            kept = [p for p in presets if p.id != preset_id]
            if len(kept) == len(presets):
                return False
            self._write(kept)
            return True
