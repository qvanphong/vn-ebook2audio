"""The machines that make speech: this server and any number of voice workers.

A Node is one of them. A conversion spreads its chapters over the nodes it was given, and each node is a lane that
takes the next chapter whenever it is free, so faster machines do more of the book."""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from .tts import MODEL_MODES, ModelPool, VieneuEngine
from .model import Voice

LOCAL_ID = "local"
PROBE_SECONDS = 3.0


@dataclass(frozen=True)
class WorkerConfig:
    id: str
    name: str
    url: str
    token: str | None = None
    enabled: bool = True
    batch: int = 1  # texts sent in one request; 1 keeps the one-sentence-per-request behaviour


class Node:
    """One machine. `pool` is where its models run (here, or behind a worker's address)."""

    def __init__(self, id: str, name: str, pool: ModelPool, config: WorkerConfig | None, enabled: bool, batch: int = 1):
        self.id, self.label, self.pool, self.config, self.enabled = id, name, pool, config, enabled
        self.batch = batch

    @property
    def kind(self) -> str:
        return "local" if self.config is None else "remote"

    def engine(self, spec: Voice, report=lambda _message: None) -> VieneuEngine:
        """Get this machine ready for `spec` (download and load the model, send a missing cloned voice), then
        return an engine that speaks it. Blocks while the model loads; raises LaneError if it cannot."""
        self.pool.prepare(spec, report)
        return VieneuEngine(self.pool, spec, self.batch)


def probe(url: str, token: str | None, timeout: float = PROBE_SECONDS) -> dict:
    """Ask a worker who it is. Always returns a dict with `ok`; never raises for network or protocol trouble."""
    from .worker import SUPPORTED_PROTOCOLS

    started = time.monotonic()
    try:
        response = httpx.get(f"{url}/health", headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=timeout)
    except httpx.TransportError as exc:
        return {"ok": False, "error": f"cannot reach {url} ({type(exc).__name__}). Is the worker running, and is the address right?"}
    if response.status_code == 401:
        return {"ok": False, "error": "the worker rejected the token"}
    if response.status_code != 200:
        return {"ok": False, "error": f"the worker answered {response.status_code}"}
    try:
        health = response.json()
    except ValueError:
        return {"ok": False, "error": "that address answered, but it is not a voice worker"}
    if health.get("protocol") not in SUPPORTED_PROTOCOLS:
        return {"ok": False, "error": f"the worker speaks protocol {health.get('protocol')}, this server understands "
                                      f"{' and '.join(map(str, SUPPORTED_PROTOCOLS))}"}
    states = None
    if (health.get("protocol") or 0) >= 3:
        try:
            listing = httpx.get(f"{url}/models", headers={"Authorization": f"Bearer {token}"} if token else {}, timeout=timeout)
            states = listing.json()["models"] if listing.status_code == 200 else None
        except (httpx.TransportError, ValueError, KeyError):
            pass
    return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000), "name": health.get("name"),
            "states": states, "device": health.get("device"),
            "models": {m: bool(health.get("models", {}).get(m, {}).get("ready")) for m in MODEL_MODES},
            "capabilities": health.get("capabilities") or {"synthesize": True, "infer": [], "clone": True}}
