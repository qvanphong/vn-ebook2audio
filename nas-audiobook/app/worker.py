"""A voice worker: runs the models and answers synthesis requests over HTTP, so the machine that holds the
library (a NAS) can hand the heavy work to a faster machine. Start it with: python -m app.worker"""
from __future__ import annotations

import hmac
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel

from . import catalog, tts
from .config import Settings

PROTOCOL = 3
SUPPORTED_PROTOCOLS = (1, 2, 3)
MAX_TEXT_CHARS = 6000
INFER_MODELS = ("nano",)  # what can be run from phonemes alone


class Inference(BaseModel):
    model: str
    phonemes: str
    voice: str | None = None
    steps: int | None = None
    cfg: float | None = None
    speed: float = 1.0
    seed: int | None = None


class Synthesis(BaseModel):
    model: str
    text: str
    voice: str | None = None
    temperature: float = 0.8


def create_worker_app(pool: tts.ModelPool, default_model: str, token: str | None = None,
                      accepts: str = "both", name: str | None = None) -> FastAPI:
    """accepts="phonemes" makes this worker behave like a phone: it only runs neural networks on phonemes."""
    text_ok = accepts != "phonemes"
    def auth(authorization: str | None = Header(None)) -> None:
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "missing or wrong worker token")

    app = FastAPI(title="voice worker", dependencies=[Depends(auth)])

    def loading(exc: tts.ModelLoading) -> HTTPException:
        return HTTPException(503, {"loading": exc.error is None, "message": str(exc)})

    def can_run(model: str) -> bool:
        return text_ok or model in INFER_MODELS

    def model_states() -> dict:
        """What this machine has of each model. `supported` is whether it could ever run it (the package is
        installed, and a phone-style worker only runs what it can run from phonemes)."""
        return {m: {**s, "supported": bool(s["supported"]) and can_run(m)} for m, s in pool.status().items()}

    @app.get("/health")
    def health():
        return {"ok": True, "protocol": PROTOCOL, "name": name, "models": {m: {"ready": pool.ready(m)} for m in tts.MODEL_MODES},
                "capabilities": {"synthesize": text_ok, "infer": list(INFER_MODELS), "clone": text_ok}}

    @app.get("/models")
    def models():
        """Protocol 3: every model this server knows, and whether this machine has it, is fetching it, or can run it."""
        return {"models": model_states()}

    @app.post("/models/{model}/ensure", status_code=202)
    def ensure_model(model: str):
        """Protocol 3: make sure this machine has the model downloaded and loaded. Returns at once; poll GET /models.
        Asking again while it is already on its way, or once it is ready, changes nothing."""
        if model not in tts.MODEL_MODES:
            raise HTTPException(400, f"unknown model {model!r}, expected one of {sorted(tts.MODEL_MODES)}")
        state = model_states()[model]
        if not state["supported"]:
            raise HTTPException(501, f"this worker cannot run the {model} model")
        pool.start_loading(model)
        return {"model": model, **model_states()[model]}

    @app.get("/voices")
    def voices(model: str):
        try:
            result = pool.voices(model)
            return {"voices": result.voices, "ready": result.ready, "error": result.error}
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except tts.ModelLoading as exc:
            raise loading(exc)

    @app.post("/synthesize")
    def synthesize(request: Synthesis):
        """Mono 16-bit little-endian PCM at the model's sample rate (see the X-Sample-Rate header)."""
        if not text_ok:
            raise HTTPException(501, "this worker only accepts phonemes: use /infer")
        if len(request.text) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"text is longer than {MAX_TEXT_CHARS} characters")
        try:
            pool.ensure(request.model)
            audio = pool.synthesize(request.model, request.text, request.voice, request.temperature)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except tts.ModelLoading as exc:
            raise loading(exc)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        return Response(pcm, media_type="application/octet-stream",
                        headers={"X-Sample-Rate": str(tts.SAMPLE_RATES[request.model])})

    @app.post("/infer")
    def infer(request: Inference):
        """One already-phonemised chunk (protocol 2): mono 16-bit PCM, trimmed and faded, rate in X-Sample-Rate."""
        if len(request.phonemes) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"phonemes are longer than {MAX_TEXT_CHARS} characters")
        try:
            pool.ensure(request.model)
            audio = pool.infer_phonemes(request.model, request.phonemes, request.voice, steps=request.steps,
                                        cfg=request.cfg, speed=request.speed, seed=request.seed)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except tts.Unsupported as exc:
            raise HTTPException(501, str(exc))
        except tts.ModelLoading as exc:
            raise loading(exc)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        return Response(pcm, media_type="application/octet-stream",
                        headers={"X-Sample-Rate": str(tts.SAMPLE_RATES[request.model])})

    @app.post("/clones", status_code=201)
    def add_clone(name: str = Form(...), file: UploadFile = File(...)):
        if not text_ok:
            raise HTTPException(501, "this worker cannot clone voices")
        with tempfile.TemporaryDirectory() as d:
            upload = Path(d) / "upload"
            with upload.open("wb") as out:
                shutil.copyfileobj(file.file, out)
            try:
                pool.add_clone(name, upload, default_model)
            except tts.CloneError as exc:
                raise HTTPException(400, str(exc))
            except tts.ModelLoading as exc:
                raise loading(exc)
        return {"name": name.strip()}

    @app.delete("/clones/{name}", status_code=204)
    def remove_clone(name: str):
        if not pool.remove_clone(name):
            raise HTTPException(404, "no such cloned voice")

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    catalog.configure_cache_env()
    settings = Settings.from_env()
    token = os.environ.get("WORKER_TOKEN") or None
    pool = tts.ModelPool.local(settings.threads, settings.precision, tts.CloneStore(settings.voices_dir), settings.voice_cache)
    try:
        models = catalog.parse_model_list(os.environ.get("PRELOAD_MODELS"))  # passive by default: the server says what it needs
    except ValueError as exc:
        raise SystemExit(str(exc))

    def preload() -> None:
        for model in models:
            try:
                pool.get(model)
                logging.getLogger("worker").info("model %s ready", model)
            except Exception as exc:
                logging.getLogger("worker").warning("model %s could not be loaded at start: %s", model, exc)

    threading.Thread(target=preload, daemon=True).start()
    if token is None:
        logging.getLogger("worker").warning("WORKER_TOKEN is not set: anyone who can reach this port can use the worker")
    accepts = os.environ.get("WORKER_ACCEPTS", "both")
    if accepts not in ("both", "phonemes"):
        raise SystemExit("WORKER_ACCEPTS must be both or phonemes")
    name = os.environ.get("WORKER_NAME") or None
    uvicorn.run(create_worker_app(pool, settings.model, token, accepts, name), host="0.0.0.0",
                port=int(os.environ.get("WORKER_PORT", "8780")), log_level="info")


if __name__ == "__main__":
    main()
