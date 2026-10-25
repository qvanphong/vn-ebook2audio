"""A voice worker: runs the models and answers synthesis requests over HTTP, so the machine that holds the
library (a NAS) can hand the heavy work to a faster machine. Start it with: python -m app.worker"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Sequence

import numpy as np
import uvicorn
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Response, UploadFile
from pydantic import BaseModel

from . import accel, catalog, params, tts
from .config import Settings

PROTOCOL = 4  # batching is a capability (health: capabilities.batch), not a new protocol, so older servers still work
SUPPORTED_PROTOCOLS = (1, 2, 3, 4)
MAX_TEXT_CHARS = 6000
INFER_MODELS = ("nano",)  # what can be run from phonemes alone


class Inference(BaseModel):
    model: str
    phonemes: str
    voice: str | None = None
    steps: int | None = None
    cfg: float | None = None
    sway: float = 0.0
    speed: float = 1.0
    seed: int | None = None


class Synthesis(BaseModel):
    model: str
    text: str
    voice: str | None = None
    params: dict[str, float | int | None] = {}  # any setting the model has (app/params.py)
    temperature: float | None = None  # what servers before `capabilities.params` send


class SynthesisBatch(BaseModel):
    model: str
    texts: list[str]
    voice: str | None = None
    params: dict[str, float | int | None] = {}
    temperature: float | None = None


def options_for(request: Synthesis | SynthesisBatch) -> dict[str, float | int]:
    """The engine settings in a request, checked against what the model has. A bad one is a 400, not a guess."""
    given = dict(request.params)
    if request.temperature is not None and any(p.key == "temperature" for p in params.PARAMS.get(request.model, ())):
        given.setdefault("temperature", request.temperature)
    try:
        return params.engine_kwargs(params.resolve(request.model, given))
    except params.ParamError as exc:
        raise HTTPException(400, str(exc))


def create_worker_app(pool: tts.ModelPool, default_model: str, token: str | None = None,
                      accepts: str = "both", name: str | None = None, offered: Sequence[str] | None = None) -> FastAPI:
    """accepts="phonemes" makes this worker behave like a phone: it only runs neural networks on phonemes.
    `offered` is the worker's own choice of models to serve (None: all of them). The worker fetches and loads them
    itself, the first time a request needs one or at start; the server only ever asks for audio."""
    text_ok = accepts != "phonemes"
    serves = set(tts.MODEL_MODES if offered is None else offered)
    clone_model = default_model if default_model in serves else next((m for m in tts.MODEL_MODES if m in serves and m not in tts.NO_CLONE), None)
    def auth(authorization: str | None = Header(None)) -> None:
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "missing or wrong worker token")

    app = FastAPI(title="voice worker", dependencies=[Depends(auth)])

    def loading(exc: tts.ModelLoading) -> HTTPException:
        return HTTPException(503, {"loading": exc.error is None, "message": str(exc)})

    def can_run(model: str) -> bool:
        return model in serves and (text_ok or model in INFER_MODELS)

    def serve(model: str) -> None:
        if model in tts.MODEL_MODES and not can_run(model):
            raise HTTPException(501, f"this worker does not serve the {model} model")

    def model_states() -> dict:
        """What this machine has of each model. `offered` is whether the worker chose to serve it, and `supported`
        whether it could run it at all (the package is installed, and a phone-style worker only runs what it can run
        from phonemes). `device` is where the model runs on this machine, which the worker decides."""
        return {m: {**s, "offered": m in serves, "supported": bool(s["supported"]) and can_run(m)}
                for m, s in pool.status().items()}

    @app.get("/health")
    def health():
        return {"ok": True, "protocol": PROTOCOL, "name": name, "device": accel.status(), "models": {m: {"ready": pool.ready(m)} for m in tts.MODEL_MODES},
                "capabilities": {"synthesize": text_ok, "infer": list(INFER_MODELS), "clone": text_ok, "batch": text_ok,
                                 "params": text_ok}}

    @app.get("/models")
    def models():
        """Every model this server knows, and whether this machine serves it, has it, is fetching it, and on which device."""
        return {"models": model_states()}

    @app.get("/voices")
    def voices(model: str):
        serve(model)
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
        serve(request.model)
        options = options_for(request)
        try:
            pool.ensure(request.model)
            audio = pool.synthesize(request.model, request.text, request.voice, options)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except tts.ModelLoading as exc:
            raise loading(exc)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        return Response(pcm, media_type="application/octet-stream",
                        headers={"X-Sample-Rate": str(tts.SAMPLE_RATES[request.model])})

    @app.post("/synthesize_batch")
    def synthesize_batch(request: SynthesisBatch):
        """Several texts in one request (capability `batch`). The body is the mono 16-bit PCM of all of them one after the
        other; X-Sample-Counts lists how many samples each text has, in order."""
        if not text_ok:
            raise HTTPException(501, "this worker only accepts phonemes: use /infer")
        if not 1 <= len(request.texts) <= tts.MAX_BATCH:
            raise HTTPException(400, f"send between 1 and {tts.MAX_BATCH} texts per request")
        if any(len(text) > MAX_TEXT_CHARS for text in request.texts):
            raise HTTPException(400, f"text is longer than {MAX_TEXT_CHARS} characters")
        serve(request.model)
        options = options_for(request)
        try:
            pool.ensure(request.model)
            audios = pool.synthesize_many(request.model, request.texts, request.voice, options)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except tts.ModelLoading as exc:
            raise loading(exc)
        pcm = b"".join((np.clip(audio, -1.0, 1.0) * 32767.0).astype("<i2").tobytes() for audio in audios)
        return Response(pcm, media_type="application/octet-stream",
                        headers={"X-Sample-Rate": str(tts.SAMPLE_RATES[request.model]),
                                 "X-Sample-Counts": ",".join(str(len(audio)) for audio in audios)})

    @app.post("/infer")
    def infer(request: Inference):
        """One already-phonemised chunk (protocol 2): mono 16-bit PCM, trimmed and faded, rate in X-Sample-Rate."""
        if len(request.phonemes) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"phonemes are longer than {MAX_TEXT_CHARS} characters")
        serve(request.model)
        try:
            pool.ensure(request.model)
            audio = pool.infer_phonemes(request.model, request.phonemes, request.voice, steps=request.steps,
                                        cfg=request.cfg, sway=request.sway, speed=request.speed,
                                        seed=request.seed)
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
        if not text_ok or clone_model is None:
            raise HTTPException(501, "this worker cannot clone voices")
        with tempfile.TemporaryDirectory() as d:
            upload = Path(d) / "upload"
            with upload.open("wb") as out:
                shutil.copyfileobj(file.file, out)
            try:
                pool.add_clone(name, upload, clone_model)
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
    ap = argparse.ArgumentParser(description="A voice worker for ebook2audio.")
    ap.add_argument("--device", help="auto (default), cpu, cuda, directml, coreml or openvino; overrides TTS_DEVICE")
    ap.add_argument("--models", help="the models this worker serves, for example nano,turbo (default: all); overrides WORKER_MODELS")
    args = ap.parse_args()
    catalog.configure_cache_env()
    settings = Settings.from_env()
    try:
        accel.configure(args.device or settings.device)
    except accel.DeviceError as exc:
        raise SystemExit(str(exc))
    token = os.environ.get("WORKER_TOKEN") or None
    pool = tts.ModelPool.local(settings.threads, settings.precision, tts.CloneStore(settings.voices_dir), settings.voice_cache)
    try:
        offered = catalog.parse_offered(args.models or os.environ.get("WORKER_MODELS"))
        models = catalog.parse_model_list(os.environ.get("PRELOAD_MODELS"))  # fetched at start; the rest on first use
    except ValueError as exc:
        raise SystemExit(str(exc))
    if (left := [m for m in models if m not in offered]):
        raise SystemExit(f"PRELOAD_MODELS names {', '.join(left)}, which WORKER_MODELS does not serve")

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
    uvicorn.run(create_worker_app(pool, settings.model, token, accepts, name, offered), host="0.0.0.0",
                port=int(os.environ.get("WORKER_PORT", "8780")), log_level="info")


if __name__ == "__main__":
    main()
