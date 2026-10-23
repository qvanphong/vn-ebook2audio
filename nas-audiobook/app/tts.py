from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from . import accel, catalog, onnxpaths
from .model import Voice

MODEL_MODES = {"turbo": "v3turbo", "nano": "v3nano", "english": "supertonic"}
SAMPLE_RATES = {"turbo": 48000, "nano": 24000, "english": 44100}
NO_CLONE = frozenset({"english"})  # models with a fixed set of voices
ENGLISH_STEPS = 8
SMOKE_TEXT = {"nano": "Xin chào.", "turbo": "Xin chào.", "english": "Hello."}  # a first run that proves a model works
REFERENCE_SECONDS = 15
PRECISIONS = ("fp32", "int8")


class Engine(Protocol):
    sample_rate: int

    def synthesize(self, text: str, dialog: bool = False) -> np.ndarray: ...


class EngineFactory(Protocol):
    def __call__(self, voice: Voice) -> Engine: ...


class CloneError(Exception):
    pass


class LaneError(RuntimeError):
    """One machine cannot do this job (offline, missing model or voice). Another machine still may."""


class VoiceMissing(LaneError):
    pass


class Unsupported(Exception):
    """The active backend cannot do this (for example phoneme input for the Turbo model)."""


@dataclass(frozen=True)
class VoiceList:
    voices: list[dict]
    ready: bool
    error: str | None = None


class ModelLoading(LaneError):
    """The model is still loading (first run downloads it) or failed to load."""

    def __init__(self, model: str, error: str | None = None):
        self.model, self.error = model, error
        super().__init__(error or f"the {model} model is still loading")


class CloneStore:
    """Reference clips for cloned voices, kept as files so they survive restarts and work with every model."""

    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._index = directory / "index.json"

    def names(self) -> dict[str, Path]:
        if not self._index.exists():
            return {}
        return {n: self.dir / f for n, f in json.loads(self._index.read_text(encoding="utf8")).items()}

    def add(self, name: str, wav: Path) -> Path:
        index = {n: p.name for n, p in self.names().items()}
        stem = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE).strip("_") or "voice"
        target = self.dir / f"{stem}.wav"
        target.write_bytes(wav.read_bytes())
        index[name] = target.name
        self._index.write_text(json.dumps(index, ensure_ascii=False), encoding="utf8")
        return target

    def remove(self, name: str) -> bool:
        index = {n: p.name for n, p in self.names().items()}
        if name not in index:
            return False
        (self.dir / index.pop(name)).unlink(missing_ok=True)
        self._index.write_text(json.dumps(index, ensure_ascii=False), encoding="utf8")
        return True


def normalize_reference(src: Path, dst: Path) -> None:
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-t", str(REFERENCE_SECONDS), "-ac", "1", "-ar", "24000", str(dst)],
        capture_output=True, check=False,
    )
    if proc.returncode != 0:
        raise CloneError(f"could not read the audio file: {proc.stderr.decode(errors='replace')[-200:]}")


class Backend(Protocol):
    """Where a model really runs. load() returns a handle with list_preset_voices(), infer(), add_voice() and
    remove_voice(), which is what the VieNeu object itself offers, so a remote worker can stand in for it."""

    def load(self, model: str): ...


class LocalHandle:
    """A VieNeu model running in this process."""

    can_clone = True

    def __init__(self, model: str, tts):
        self.model, self._tts = model, tts

    def list_preset_voices(self):
        return self._tts.list_preset_voices()

    def voice_ids(self) -> list[str]:
        """Every voice this model can speak right now, cloned ones included."""
        return [vid for _label, vid in self._tts.list_preset_voices()]

    def infer(self, text, voice=None, temperature=0.8, **kwargs):
        return self._tts.infer(text, voice=voice, temperature=temperature, **kwargs)

    def add_voice(self, name, ref_audio):
        return self._tts.add_voice(name, ref_audio)

    def remove_voice(self, name):
        return self._tts.remove_voice(name)

    def infer_phonemes(self, phonemes, voice=None, steps=None, cfg=None, speed=1.0, seed=None):
        """One already-phonemised chunk, the way the Nano engine runs it (see docs/phone-worker.md)."""
        if self.model != "nano":
            raise Unsupported("phoneme input is only implemented for the nano model")
        speaker, style = self._tts._resolve_voice(voice, None)
        return self._tts.engine.infer(
            phonemes, speaker, style, steps=steps or self._tts.default_steps,
            cfg=self._tts.default_cfg if cfg is None else cfg, sway=0.0, speed=speed, seed=seed)


class SupertonicHandle:
    """The English model (Supertone's Supertonic) in this process: ten built-in voices, no cloning."""

    can_clone = False
    DEFAULT_VOICE = "F1"

    def __init__(self, tts):
        self._tts = tts

    def voice_ids(self) -> list[str]:
        return list(self._tts.voice_style_names)

    def list_preset_voices(self):
        return [(f"{v} — {'Female' if v.startswith('F') else 'Male'}", v) for v in self.voice_ids()]

    def infer(self, text, voice=None, temperature=0.8, **_):
        """`temperature` belongs to the Vietnamese models and is ignored here."""
        name = voice or self.DEFAULT_VOICE
        if name not in self.voice_ids():
            raise ValueError(f"the English model has no voice {name!r}; it has {', '.join(self.voice_ids())}")
        wav, _duration = self._tts.synthesize(text, voice_style=self._tts.get_voice_style(name),
                                              total_steps=ENGLISH_STEPS, lang="en")
        return np.asarray(wav, dtype=np.float32).reshape(-1)

    def add_voice(self, name, ref_audio):
        raise Unsupported("the English model cannot clone voices")

    def remove_voice(self, name):
        pass


class LocalBackend:
    def __init__(self, threads: int, precision: str):
        self.threads, self.precision = threads, precision

    def downloaded(self, model: str) -> bool:
        return catalog.downloaded(model, self.precision)

    def bytes_on_disk(self, model: str) -> int:
        return catalog.bytes_on_disk(model, self.precision)

    def supported(self, model: str) -> bool:
        return catalog.package_installed(model)

    def load(self, model: str):
        onnxpaths.install()
        return accel.load_checked(lambda: self._load(model), lambda handle: handle.infer(SMOKE_TEXT[model]), model,
                                  catalog.CATALOG[model].accelerators)

    def device(self, model: str) -> dict:
        """Where this model runs, or will be tried: its own record once loaded, else the plan for this machine."""
        done = accel.status()["models"].get(model)
        return done or {"device": accel.device_for(catalog.CATALOG[model].accelerators), "fallback": False, "note": None}

    def _load(self, model: str):
        if model == "english":
            try:
                from supertonic import TTS
            except ImportError as exc:
                raise RuntimeError("the English model needs the supertonic package: pip install supertonic==1.3.1") from exc
            return SupertonicHandle(TTS(intra_op_num_threads=self.threads))
        from vieneu import Vieneu

        kwargs = {"precision": self.precision} if model == "turbo" else {}
        return LocalHandle(model, Vieneu(mode=MODEL_MODES[model], threads=self.threads, **kwargs))


class ModelPool:
    """Loads each model once through a backend and keeps cloned voices enrolled in every loaded model."""

    def __init__(self, backend: Backend, clones: CloneStore | None = None, voice_cache: Path | None = None):
        self.backend, self.clones = backend, clones
        self._generation = 0
        self._presets_now: dict[str, list[dict]] = {}
        self._voice_cache = voice_cache
        self._lock = threading.RLock()
        self._models: dict[str, object] = {}
        self._infer_locks = {m: threading.Lock() for m in MODEL_MODES}
        self._load_locks = {m: threading.Lock() for m in MODEL_MODES}
        self._loading: set[str] = set()
        self._errors: dict[str, str] = {}
        self._baseline: dict[str, int] = {}

    @classmethod
    def local(cls, threads: int, precision: str, clones: CloneStore | None = None,
              voice_cache: Path | None = None) -> "ModelPool":
        return cls(LocalBackend(threads, precision), clones, voice_cache)

    def _ask(self, name: str, model: str, default=None):
        """An optional question to the backend (is it downloaded, how much has arrived, is it supported here)."""
        fn = getattr(self.backend, name, None)
        try:
            return fn(model) if fn else default
        except Exception:
            return default

    def status(self) -> dict:
        """Per model: ready (in memory), loading, and what is on disk. `state` is one of ready, loading, downloading,
        error, downloaded (on disk, not loaded) or absent (nothing yet), so the page can say what a conversion will do."""
        out = {}
        for m in MODEL_MODES:
            ready, loading, error = m in self._models, m in self._loading, self._errors.get(m)
            on_disk = self._ask("downloaded", m)
            if ready:
                state = "ready"
            elif loading:
                state = "loading" if on_disk else "downloading"
            elif error:
                state = "error"
            else:
                state = "downloaded" if on_disk else "absent"
            arrived = max(0, self._ask("bytes_on_disk", m, 0) - self._baseline.get(m, 0)) if state == "downloading" else 0
            out[m] = {"ready": ready, "loading": loading, "error": error, "downloaded": on_disk, "state": state,
                      "supported": self._ask("supported", m, True), "bytes": arrived, "device": self._ask("device", m),
                      "size_mb": catalog.CATALOG[m].size_mb}
        return out

    def swap_backend(self, backend: Backend) -> None:
        """Run models somewhere else from now on. Loaded models are dropped and load again on first use."""
        with self._lock:
            self.backend = backend
            self._generation += 1
            self._models.clear()
            self._presets_now.clear()
            self._errors.clear()

    def ready(self, model: str) -> bool:
        return model in self._models

    def start_loading(self, model: str) -> None:
        """Begin loading a model in the background unless it is ready or already loading."""
        if model not in MODEL_MODES:
            raise ValueError(f"unknown model {model!r}, expected one of {sorted(MODEL_MODES)}")
        with self._lock:
            if model not in self._loading and model not in self._models:
                self._baseline[model] = self._ask("bytes_on_disk", model, 0)
                self._loading.add(model)
                self._errors.pop(model, None)
                threading.Thread(target=self._background_load, args=(model,), daemon=True).start()

    def ensure(self, model: str) -> None:
        """Make sure a model is loading or loaded; raise ModelLoading until it is ready."""
        self.start_loading(model)
        if not self.ready(model):
            raise ModelLoading(model, self._errors.get(model))

    def _background_load(self, model: str) -> None:
        try:
            self.get(model)
        except Exception as exc:
            self._errors[model] = f"{type(exc).__name__}: {exc}"
        finally:
            self._loading.discard(model)

    def get(self, model: str):
        """The loaded model, loading it if needed. Only a per-model lock is held while loading, so the
        short state lock (and with it voices() and start_loading()) never waits for a slow load."""
        if model not in MODEL_MODES:
            raise ValueError(f"unknown model {model!r}, expected one of {sorted(MODEL_MODES)}")
        while True:
            if model in self._models:
                return self._models[model]
            with self._load_locks[model]:
                if model in self._models:
                    continue
                generation, backend = self._generation, self.backend
                tts = backend.load(model)
                with self._lock:
                    if generation != self._generation:
                        continue
                    wanted = self.clones.names() if self.clones else {}
                    if wanted and getattr(tts, "can_clone", True):
                        have = set(tts.voice_ids()) if hasattr(tts, "voice_ids") else set()
                        for name, wav in wanted.items():
                            if name not in have:
                                tts.add_voice(name, wav)
                    self._models[model] = tts
                self._remember_presets(model, tts)

    def _cloned(self) -> set[str]:
        return set(self.clones.names()) if self.clones else set()

    def prepare(self, spec: Voice, report=lambda _message: None) -> None:
        """Make this pool able to speak `spec`: the model downloaded and loaded, and every voice it names present.
        Presets must already exist; a cloned voice that is missing (a worker that never saw it) is uploaded."""
        info = catalog.CATALOG[spec.model]
        if self._ask("downloaded", spec.model, True) is False:
            report(f"downloading the {info.label} model (about {info.size_mb} MB), then loading it")
        else:
            report(f"loading the {info.label} model")
        wait = getattr(self.backend, "await_model", None)
        if wait is not None:
            wait(spec.model, report)
        handle = self.get(spec.model)
        names = list(dict.fromkeys(n for n in (spec.voice, spec.dialog_voice) if n))
        if not names:
            return
        report("checking voices")
        have = set(handle.voice_ids())
        for name in names:
            if name in have:
                continue
            wav = self.clones.names().get(name) if self.clones else None
            if wav is None or not getattr(handle, "can_clone", True):
                raise VoiceMissing(f"the voice {name!r} does not exist on this machine")
            report(f"sending the cloned voice {name!r}")
            handle.add_voice(name, wav)

    def can_clone(self, model: str) -> bool:
        if model in NO_CLONE:
            return False
        handle = self._models.get(model)
        return True if handle is None else bool(getattr(handle, "can_clone", True))

    def _presets(self, tts) -> list[dict]:
        cloned = self._cloned()
        return [{"id": vid, "label": label, "cloned": False} for label, vid in tts.list_preset_voices() if vid not in cloned]

    def _remember_presets(self, model: str, tts) -> None:
        presets = self._presets(tts)
        self._presets_now[model] = presets
        if not self._voice_cache:
            return
        known = self._read_cache()
        known[model] = presets
        self._voice_cache.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._voice_cache.with_name(self._voice_cache.name + ".part")
        tmp.write_text(json.dumps(known, ensure_ascii=False), encoding="utf8")
        os.replace(tmp, self._voice_cache)

    def _read_cache(self) -> dict[str, list[dict]]:
        try:
            return json.loads(self._voice_cache.read_text(encoding="utf8")) if self._voice_cache else {}
        except (OSError, ValueError):
            return {}

    def voices(self, model: str) -> VoiceList:
        """Voices for a model. Before the model has loaded, answer from the last known list on disk."""
        self.start_loading(model)
        cloned = [{"id": n, "label": f"{n} (cloned)", "cloned": True}
                  for n in sorted(self._cloned()) if self.can_clone(model)]
        if self.ready(model) and model in self._presets_now:
            return VoiceList(self._presets_now[model] + cloned, True)
        remembered = self._read_cache().get(model)
        error = self._errors.get(model)
        if remembered is None:
            raise ModelLoading(model, error)
        return VoiceList(remembered + cloned, False, error)

    def add_clone(self, name: str, upload: Path, default_model: str) -> None:
        name = name.strip()
        if not name:
            raise CloneError("the voice needs a name")
        self.ensure(default_model)
        if not self.can_clone(default_model):
            raise CloneError("the voice worker in use cannot clone voices; choose This server in Settings to clone")
        with self._lock:
            if any(v["id"] == name and not v["cloned"] for v in self.voices(default_model).voices):
                raise CloneError(f"{name!r} is the name of a built-in voice")
            with tempfile.TemporaryDirectory() as d:
                ref = Path(d) / "ref.wav"
                normalize_reference(upload, ref)
                self.get(default_model).add_voice(name, ref)
                for model, tts in self._models.items():
                    if model != default_model and getattr(tts, "can_clone", True):
                        tts.add_voice(name, ref)
                self.clones.add(name, ref)

    def remove_clone(self, name: str) -> bool:
        with self._lock:
            removed = self.clones.remove(name)
            for tts in self._models.values():
                tts.remove_voice(name)
            return removed

    def synthesize(self, model: str, text: str, voice: str | None, temperature: float) -> np.ndarray:
        """One synthesis call. Jobs and voice samples share each model, so calls take turns."""
        tts = self.get(model)
        with self._infer_locks[model]:
            audio = tts.infer(text, voice=voice, temperature=temperature)
        return np.asarray(audio, dtype=np.float32).reshape(-1)

    def infer_phonemes(self, model: str, phonemes: str, voice: str | None, **options) -> np.ndarray:
        """One chunk of phonemes to audio, for callers that do the text work themselves."""
        tts = self.get(model)
        if not hasattr(tts, "infer_phonemes"):
            raise Unsupported("this backend does not accept phonemes")
        with self._infer_locks[model]:
            audio = tts.infer_phonemes(phonemes, voice=voice, **options)
        return np.asarray(audio, dtype=np.float32).reshape(-1)

    def factory(self) -> EngineFactory:
        return lambda voice: VieneuEngine(self, voice)


class VieneuEngine:
    def __init__(self, pool: ModelPool, voice: Voice):
        self._pool, self._voice = pool, voice
        pool.get(voice.model)
        self.sample_rate = SAMPLE_RATES[voice.model]

    def synthesize(self, text: str, dialog: bool = False) -> np.ndarray:
        name = self._voice.dialog_voice if dialog and self._voice.dialog_voice else self._voice.voice
        return self._pool.synthesize(self._voice.model, text, name, self._voice.temperature)
