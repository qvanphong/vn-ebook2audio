"""Which machines make the speech: this server, and any number of voice workers. Chosen in the UI, saved on disk."""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse

from . import accel, catalog
from .fleet import LOCAL_ID, Node, WorkerConfig, probe
from .tts import MAX_BATCH, CloneStore, ModelPool
from .remote import WorkerError

STATUS_CACHE_SECONDS = 4.0


class ConfigError(ValueError):
    pass


class WorkerOwnsModels(ConfigError):
    pass


@dataclass(frozen=True)
class EngineConfig:
    local_enabled: bool = True
    workers: tuple[WorkerConfig, ...] = ()
    local_batch: int = 1


PoolFactory = Callable[[WorkerConfig | None], ModelPool]


def normalize_url(url: str | None) -> str:
    text = (url or "").strip()
    if text and "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ConfigError("the worker address must look like http://192.168.1.20:8780")
    return text.rstrip("/")


def local_available() -> bool:
    return importlib.util.find_spec("vieneu") is not None


def check_batch(value: int) -> int:
    if not 1 <= value <= MAX_BATCH:
        raise ConfigError(f"texts per request must be between 1 and {MAX_BATCH}, not {value}")
    return value


def default_config(backend: str, worker_url: str | None, worker_token: str | None) -> EngineConfig:
    """What TTS_BACKEND, TTS_WORKER_URL and TTS_WORKER_TOKEN ask for, used until something is saved in the UI."""
    if backend not in ("local", "remote"):
        raise ConfigError(f"TTS_BACKEND must be local or remote, not {backend!r}")
    if backend == "local":
        return EngineConfig(True, ())
    if not worker_url:
        raise ConfigError("TTS_BACKEND=remote needs TTS_WORKER_URL, for example http://192.168.1.20:8780")
    return EngineConfig(False, (WorkerConfig("worker", "Voice worker", normalize_url(worker_url), worker_token, True),))


def pool_factory(threads: int, precision: str, clones: CloneStore, voice_cache: Path) -> PoolFactory:
    """Real pools: the models here for the local machine, a RemoteBackend for each worker."""
    from .remote import RemoteBackend
    from .tts import LocalBackend

    def make(worker: WorkerConfig | None) -> ModelPool:
        if worker is None:
            return ModelPool(LocalBackend(threads, precision), clones, voice_cache)
        return ModelPool(RemoteBackend(worker.url, worker.token), clones,
                         voice_cache.with_name(f"voices-{worker.id}.json"))

    return make


class EngineConfigStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self, default: EngineConfig) -> EngineConfig:
        try:
            data = json.loads(self.path.read_text(encoding="utf8"))
        except (OSError, ValueError):
            return default
        if "backend" in data:  # the single-worker format of earlier versions
            return default_config(data["backend"], data.get("worker_url"), data.get("worker_token"))
        try:
            workers = tuple(WorkerConfig(w["id"], w["name"], w["url"], w.get("token"), bool(w.get("enabled", True)),
                                         int(w.get("batch", 1)))
                            for w in data.get("workers", []))
            return EngineConfig(bool(data.get("local_enabled", True)), workers, int(data.get("local_batch", 1)))
        except (KeyError, TypeError, ValueError):
            return default

    def save(self, config: EngineConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(json.dumps(asdict(config), ensure_ascii=False), encoding="utf8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)


class EngineController:
    """Owns the list of machines. Editing it never disturbs a conversion that already started: that keeps the
    machines it began with, and a machine you remove just stops being offered to new ones."""

    def __init__(self, store: EngineConfigStore, default: EngineConfig, make_pool: PoolFactory):
        self.store, self._make_pool = store, make_pool
        self._lock = threading.RLock()
        self._probes: dict[str, tuple[float, dict]] = {}
        self.config = store.load(default)
        self._nodes = self._build(self.config, {})

    # --- the machines ---------------------------------------------------------------------

    def _build(self, config: EngineConfig, old: dict[str, Node]) -> dict[str, Node]:
        local = old.get(LOCAL_ID) or Node(LOCAL_ID, "This server", self._make_pool(None), None, config.local_enabled)
        local.enabled, local.batch = config.local_enabled, config.local_batch
        nodes = {LOCAL_ID: local}
        for w in config.workers:
            kept = old.get(w.id)
            same = kept is not None and kept.config is not None and (kept.config.url, kept.config.token) == (w.url, w.token)
            nodes[w.id] = Node(w.id, w.name, kept.pool if same else self._make_pool(w), w, w.enabled, w.batch)
        return nodes

    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    @property
    def primary(self) -> Node:
        """The machine that answers for voice lists, samples and cloning: this server if it is on, else the first worker."""
        nodes = self.nodes()
        return next((n for n in nodes if n.enabled), nodes[0])

    @property
    def pool(self) -> ModelPool:
        return self.primary.pool

    def lanes(self, selected: Sequence[str] | None = None) -> list[Node]:
        """The machines a conversion uses: the ones it was asked for that still exist, or every enabled one."""
        nodes = self._nodes
        if selected is None:
            return [n for n in nodes.values() if n.enabled]
        return [nodes[i] for i in dict.fromkeys(selected) if i in nodes]

    def fetch_model(self, node_id: str, model: str) -> dict:
        """Have this server download and load a model now, without waiting. A worker is not asked: it decides which
        models it serves and fetches them itself (WORKER_MODELS, PRELOAD_MODELS, or on first use)."""
        node = self._nodes.get(node_id)
        if node is None:
            raise ConfigError("that machine is not in the list")
        if model not in catalog.CATALOG:
            raise ConfigError(f"unknown model {model!r}")
        if node.config is not None:
            raise WorkerOwnsModels(f"{node.label} chooses and downloads its own models: set WORKER_MODELS and "
                                   "PRELOAD_MODELS on that worker")
        node.pool.start_loading(model)
        return {"id": node.id, "model": model}

    def remove_clone(self, name: str) -> bool:
        removed = False
        for node in self.nodes():
            try:
                removed = node.pool.remove_clone(name) or removed
            except WorkerError:
                pass  # an offline worker keeps its copy; it is never offered the voice again because this server forgets it
        return removed

    # --- settings -------------------------------------------------------------------------

    def view(self) -> dict:
        local = self._nodes[LOCAL_ID]
        return {"local": {"available": local_available(), "enabled": local.enabled, "batch": local.batch,
                          "models": local.pool.status()},
                "workers": [{"id": w.id, "name": w.name, "url": w.url, "has_token": bool(w.token), "enabled": w.enabled,
                             "batch": w.batch}
                            for w in self.config.workers]}

    def apply(self, local_enabled: bool, workers: Sequence[dict], local_batch: int = 1) -> dict:
        if local_enabled and not local_available():
            raise ConfigError("this server does not have the voice models installed, so it cannot run them itself")
        with self._lock:
            known = {w.id: w for w in self.config.workers}
            urls: set[str] = set()
            built: list[WorkerConfig] = []
            for raw in workers:
                url = normalize_url(raw.get("url"))
                if url in urls:
                    raise ConfigError(f"{url} is in the list twice")
                urls.add(url)
                before = known.get(raw.get("id") or "")
                token = raw.get("token") or (None if raw.get("clear_token") or before is None else before.token)
                name = (raw.get("name") or "").strip() or urlparse(url).hostname
                built.append(WorkerConfig(before.id if before else uuid.uuid4().hex[:8], name, url, token,
                                          bool(raw.get("enabled", True)), check_batch(int(raw.get("batch", 1)))))
            new = EngineConfig(local_enabled, tuple(built), check_batch(local_batch))
            if not (new.local_enabled or any(w.enabled for w in new.workers)):
                raise ConfigError("turn on at least one machine, or nothing could make the speech")
            self._nodes = self._build(new, self._nodes)
            self._probes.clear()
            self.store.save(new)
            self.config = new
        return self.view()

    # --- live status ----------------------------------------------------------------------

    def test(self, worker_url: str, worker_token: str | None, worker_id: str | None = None) -> dict:
        url = normalize_url(worker_url)
        saved = next((w for w in self.config.workers if w.id == worker_id or w.url == url), None)
        token = worker_token or (saved.token if saved and saved.url == url else None)
        return probe(url, token)

    def status(self) -> list[dict]:
        """Every machine with whether it is reachable now and which models it has loaded. Workers are asked in
        parallel and answers are kept for a few seconds so a busy page does not hammer them."""
        nodes = self.nodes()
        workers = [n for n in nodes if n.config is not None]
        with ThreadPoolExecutor(max_workers=max(1, len(workers))) as pool:
            probes = dict(zip((n.id for n in workers), pool.map(self._probe, workers)))
        out = []
        for n in nodes:
            if n.config is None:
                models = n.pool.status()
                out.append({"id": n.id, "name": n.label, "kind": "local", "enabled": n.enabled, "batch": n.batch,
                            "online": local_available(),
                            "models": models, "device": accel.status(), "capabilities": {"synthesize": True, "clone": True},
                            "error": None if local_available() else "the voice models are not installed on this server"})
                continue
            p = probes[n.id]
            out.append({"id": n.id, "name": n.label, "kind": "remote", "url": n.config.url, "enabled": n.enabled,
                        "batch": n.batch,
                        "online": p["ok"], "latency_ms": p.get("latency_ms"), "error": p.get("error"),
                        "device": p.get("device"),
                        "models": p["states"] if p.get("states") else
                                  {m: {"ready": ready} for m, ready in p.get("models", {}).items()},
                        "capabilities": p.get("capabilities")})
        return out

    def _probe(self, node: Node) -> dict:
        now = time.monotonic()
        cached = self._probes.get(node.id)
        if cached and now - cached[0] < STATUS_CACHE_SECONDS:
            return cached[1]
        result = probe(node.config.url, node.config.token)
        self._probes[node.id] = (now, result)
        return result
