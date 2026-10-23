"""Backend that sends synthesis to a voice worker (app/worker.py) instead of running the model here."""
from __future__ import annotations

import time
from pathlib import Path

import httpx
import numpy as np

from . import catalog
from .frontend import shared_frontend
from .tts import SAMPLE_RATES, LaneError
from .worker import SUPPORTED_PROTOCOLS


class WorkerError(LaneError):
    pass


def _detail(response: httpx.Response) -> str:
    try:
        d = response.json()["detail"]
        return d if isinstance(d, str) else d.get("message", str(d))
    except (ValueError, KeyError, AttributeError):
        return response.text[:200] or response.reason_phrase


class RemoteBackend:
    def __init__(self, url: str, token: str | None = None, *, client: httpx.Client | None = None, retries: int = 4,
                 backoff: float = 2.0, load_timeout: float = 1800.0, sleep=time.sleep, frontend=None,
                 poll_seconds: float = 2.0):
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = client or httpx.Client(base_url=self.url, timeout=httpx.Timeout(30.0, read=300.0))
        self.retries, self.backoff, self.load_timeout, self._sleep = retries, backoff, load_timeout, sleep
        self.poll_seconds = poll_seconds
        self._frontend = frontend

    @property
    def frontend(self):
        if self._frontend is None:
            self._frontend = shared_frontend()
        return self._frontend

    def request(self, method: str, path: str, *, retry_loading: bool = True, **kwargs) -> httpx.Response:
        """One call to the worker. Network trouble and a model that is still loading are retried with a pause."""
        last = ""
        for attempt in range(self.retries + 1):
            try:
                response = self.client.request(method, path, headers=self.headers, **kwargs)
            except httpx.TransportError as exc:
                last = f"cannot reach the voice worker at {self.url} ({type(exc).__name__})"
            else:
                if response.status_code == 503 and retry_loading:
                    last = f"the voice worker is busy loading: {_detail(response)}"
                elif response.status_code >= 400:
                    raise WorkerError(f"the voice worker said {response.status_code}: {_detail(response)}")
                else:
                    return response
            if attempt < self.retries:
                self._sleep(self.backoff * (attempt + 1))
        raise WorkerError(last)

    def models(self) -> dict | None:
        """The worker's own account of each model (protocol 3), or None for an older worker."""
        try:
            response = self.client.request("GET", "/models", headers=self.headers)
        except httpx.TransportError as exc:
            raise WorkerError(f"cannot reach the voice worker at {self.url} ({type(exc).__name__})") from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise WorkerError(f"the voice worker said {response.status_code}: {_detail(response)}")
        return response.json()["models"]

    def await_model(self, model: str, report=lambda _message: None) -> None:
        """Wait until the worker can run the model, saying what it is doing on the way. The worker decides which models
        it serves and fetches them itself; this only asks for the model's voices (which is what makes a worker start
        fetching one it has not got yet) and watches the progress it reports. A worker older than protocol 3 is left
        to load() below."""
        if self.models() is None:
            return
        label = catalog.CATALOG[model].label
        deadline = time.monotonic() + self.load_timeout
        asked = False
        while True:
            states = self.models()
            state = states.get(model) if states else None
            if state is None:
                raise WorkerError(f"the worker does not know the {model} model")
            if state.get("offered") is False:
                raise WorkerError(f"the worker does not serve the {label} model")
            if state["ready"]:
                return
            if state["state"] == "error" and asked:
                raise WorkerError(f"the worker could not load the {label} model: {state['error']}")
            if not state["supported"]:
                raise WorkerError(f"the worker cannot run the {model} model")
            if not asked:
                self._nudge(model)
                asked = True
            if state["state"] == "downloading":
                done = state.get("bytes") or 0
                report(f"the worker is downloading the {label} model"
                       + (f" ({done // 1_000_000} of about {state['size_mb']} MB)" if done else f" (about {state['size_mb']} MB)"))
            elif state["state"] == "loading":
                report(f"the worker has the {label} model and is loading it")
            else:
                report(f"waiting for the worker to get the {label} model")
            if time.monotonic() > deadline:
                raise WorkerError(f"the worker did not finish getting the {label} model in time")
            self._sleep(self.poll_seconds)

    def _nudge(self, model: str) -> None:
        """Ask for the model's voices. A worker that serves the model starts getting it and answers 503 until it can."""
        try:
            response = self.client.request("GET", "/voices", params={"model": model}, headers=self.headers)
        except httpx.TransportError as exc:
            raise WorkerError(f"cannot reach the voice worker at {self.url} ({type(exc).__name__})") from exc
        if response.status_code not in (200, 503):
            raise WorkerError(f"the voice worker said {response.status_code}: {_detail(response)}")

    def load(self, model: str) -> "RemoteHandle":
        health = self.request("GET", "/health").json()
        if health.get("protocol") not in SUPPORTED_PROTOCOLS:
            raise WorkerError(f"the worker speaks protocol {health.get('protocol')}, this server understands "
                              f"{' and '.join(map(str, SUPPORTED_PROTOCOLS))}")
        caps = health.get("capabilities") or {"synthesize": True, "infer": [], "clone": True}
        if not caps.get("synthesize", True):
            if model not in caps.get("infer", []):
                raise WorkerError(f"the worker cannot run the {model} model")
            self.frontend  # fail now, with install instructions, if the text frontend is missing
        deadline = time.monotonic() + self.load_timeout
        while True:
            try:
                response = self.client.request("GET", "/voices", params={"model": model}, headers=self.headers)
            except httpx.TransportError as exc:
                raise WorkerError(f"cannot reach the voice worker at {self.url} ({type(exc).__name__})") from exc
            if response.status_code == 200 and response.json()["ready"]:
                return RemoteHandle(self, model, caps)
            if response.status_code not in (200, 503):
                raise WorkerError(f"the voice worker said {response.status_code}: {_detail(response)}")
            if response.status_code == 503 and response.json()["detail"].get("loading") is False:
                raise WorkerError(f"the worker could not load the {model} model: {_detail(response)}")
            if time.monotonic() > deadline:
                raise WorkerError(f"the worker did not finish loading the {model} model in time")
            self._sleep(2.0)


class RemoteHandle:
    """Looks like a loaded VieNeu model to ModelPool, but every call goes to the worker."""

    def __init__(self, backend: RemoteBackend, model: str, caps: dict | None = None):
        self._b, self._model = backend, model
        caps = caps or {"synthesize": True, "clone": True}
        self._text_ok = bool(caps.get("synthesize", True))
        self.can_clone = bool(caps.get("clone", True))

    def list_preset_voices(self) -> list[tuple[str, str]]:
        data = self._b.request("GET", "/voices", params={"model": self._model}).json()
        return [(v["label"], v["id"]) for v in data["voices"] if not v["cloned"]]

    def voice_ids(self) -> list[str]:
        data = self._b.request("GET", "/voices", params={"model": self._model}).json()
        return [v["id"] for v in data["voices"]]

    def infer(self, text: str, voice: str | None = None, temperature: float = 0.8, **_) -> np.ndarray:
        if not self._text_ok:
            return self._infer_via_phonemes(text, voice)
        response = self._b.request("POST", "/synthesize", json={"model": self._model, "text": text, "voice": voice,
                                                                "temperature": temperature})
        rate = int(response.headers.get("x-sample-rate", 0))
        if rate != SAMPLE_RATES[self._model]:
            raise WorkerError(f"the worker returned {rate} Hz audio, expected {SAMPLE_RATES[self._model]} Hz")
        return np.frombuffer(response.content, dtype="<i2").astype(np.float32) / 32768.0

    def _pcm(self, response: httpx.Response) -> np.ndarray:
        rate = int(response.headers.get("x-sample-rate", 0))
        if rate != SAMPLE_RATES[self._model]:
            raise WorkerError(f"the worker returned {rate} Hz audio, expected {SAMPLE_RATES[self._model]} Hz")
        return np.frombuffer(response.content, dtype="<i2").astype(np.float32) / 32768.0

    def _infer_via_phonemes(self, text: str, voice: str | None) -> np.ndarray:
        """This server does the text work (chunks, phonemes, pauses); the worker only runs the networks."""
        frontend = self._b.frontend
        chunks, gaps = frontend.chunks(text)
        wavs = []
        for chunk in chunks:
            phonemes = frontend.phonemes(chunk)
            response = self._b.request("POST", "/infer", json={"model": self._model, "phonemes": phonemes, "voice": voice})
            wavs.append(self._pcm(response))
        return frontend.join(wavs, SAMPLE_RATES[self._model], gaps)

    def add_voice(self, name: str, ref_audio) -> None:
        if not self.can_clone:
            raise WorkerError("this worker cannot clone voices")
        path = Path(ref_audio)
        self._b.request("POST", "/clones", data={"name": name}, files={"file": (path.name, path.read_bytes())})

    def remove_voice(self, name: str) -> None:
        try:
            self._b.request("DELETE", f"/clones/{name}")
        except WorkerError as exc:
            if "404" not in str(exc):
                raise
