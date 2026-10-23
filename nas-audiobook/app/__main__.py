from __future__ import annotations

import argparse
import logging
import os
import threading

import uvicorn

from . import accel, catalog
from .api import create_app
from .config import Settings
from .engine_config import ConfigError, EngineConfigStore, EngineController, default_config, pool_factory
from .jobs import JobStore, Runner
from .library import Library
from .tts import CloneStore, ModelPool
from .watcher import Watcher


def preload(pool: ModelPool, models: list[str]) -> None:
    for model in models:
        try:
            pool.get(model)
            logging.getLogger("tts").info("model %s ready", model)
        except Exception as exc:
            logging.getLogger("tts").warning("model %s is not ready yet: %s", model, exc)


def build_engine(settings: Settings) -> EngineController:
    """The machines start as the UI saved them, or as TTS_BACKEND asks for the first time."""
    try:
        default = default_config(settings.backend, settings.worker_url, settings.worker_token)
    except ConfigError as exc:
        raise SystemExit(str(exc))
    make_pool = pool_factory(settings.threads, settings.precision, CloneStore(settings.voices_dir), settings.voice_cache)
    return EngineController(EngineConfigStore(settings.engine_config), default, make_pool)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(description="ebook2audio server.")
    ap.add_argument("--device", help="cpu (default), auto, cuda, directml, coreml or openvino; overrides TTS_DEVICE")
    args = ap.parse_args()
    catalog.configure_cache_env()
    settings = Settings.from_env()
    try:
        accel.configure(args.device or settings.device)
    except accel.DeviceError as exc:
        raise SystemExit(str(exc))
    settings.ensure_dirs()
    store = JobStore(settings.db)
    engine = build_engine(settings)
    try:
        models = catalog.parse_model_list(os.environ.get("PRELOAD_MODELS"))
    except ValueError as exc:
        raise SystemExit(str(exc))
    if models:  # by default nothing is loaded or downloaded until a conversion, a voice list or a sample needs it
        threading.Thread(target=preload, args=(engine.pool, models), daemon=True).start()
    library = Library(settings.library)
    Runner(settings, store, engine, library).start()
    Watcher(settings, store).start()
    uvicorn.run(create_app(settings, store, engine.pool, library=library, engine=engine), host="0.0.0.0", port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
