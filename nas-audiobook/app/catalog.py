"""The models this server knows how to use, what each needs on disk, and how to tell whether a machine already has it.

A model is "downloaded" when its files are in the Hugging Face cache (VieNeu) or in Supertonic's own folder (English).
Nothing here downloads anything: the libraries do that the first time a model loads."""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelInfo:
    id: str
    label: str
    language: str
    description: str
    size_mb: int            # a rough download size, for the page
    package: str            # the Python package that runs it
    accelerators: tuple[str, ...] = ()  # the GPU devices (see accel.DEVICES) it can run on; it always runs on the CPU


CATALOG: dict[str, ModelInfo] = {
    # Nano is CPU only (CUDA and CoreML do not run its graph). The others are tried on the accelerator and fall back
    # to the CPU if their first run fails. Only English on CoreML has been run on real hardware.
    "nano": ModelInfo("nano", "Vietnamese · Nano", "vi", "Fast, lighter, good for long books", 280, "vieneu"),
    "turbo": ModelInfo("turbo", "Vietnamese · Turbo", "vi", "Best Vietnamese quality, about 3x slower", 620, "vieneu",
                       ("cuda", "directml")),
    "english": ModelInfo("english", "English · Supertonic", "en", "Clear and fast, 10 voices, no cloning", 390, "supertonic",
                         ("cuda", "directml", "coreml")),
}

NANO_REPO = "pnnbao-ump/VieNeu-TTS-v3-Nano"
NANO_FILES = ["text_encoder.onnx", "duration_predictor.onnx", "vector_estimator.onnx", "codec_decoder.onnx",
              "config.json", "constants.npz"]
TURBO_REPO = "pnnbao-ump/VieNeu-TTS-v3-Turbo"
TURBO_DIRS = {"fp32": "onnx_update", "int8": "onnx_int8"}
TURBO_FILES = ["config.json", "tokenizer.json", "vieneu_prefill.onnx", "vieneu_decode_step.onnx",
               "vieneu_acoustic_cached.onnx", "vieneu_backbone_shared.data", "vieneu_v3_heads.npz"]
MOSS_REPO = "OpenMOSS-Team/MOSS-Audio-Tokenizer-Nano-ONNX"
MOSS_FILES = ["moss_audio_tokenizer_decode_full.onnx", "moss_audio_tokenizer_decode_shared.data",
              "moss_audio_tokenizer_decode_step.onnx"]


def parse_model_list(raw: str | None) -> list[str]:
    """A PRELOAD_MODELS value as a list of model ids. Empty, missing or `none` means nothing is loaded at start."""
    names = [n.strip().lower() for n in (raw or "").split(",") if n.strip()]
    names = [n for n in names if n != "none"]
    unknown = [n for n in names if n not in CATALOG]
    if unknown:
        raise ValueError(f"PRELOAD_MODELS names unknown model(s) {', '.join(unknown)}; choose from {', '.join(CATALOG)} or none")
    return list(dict.fromkeys(names))


def parse_offered(raw: str | None) -> list[str]:
    """A WORKER_MODELS value as a list of model ids. Empty, missing or `all` means every model there is."""
    if (raw or "").strip().lower() in ("", "all"):
        return list(CATALOG)
    names = parse_model_list(raw)
    if not names:
        raise ValueError("WORKER_MODELS names no model; choose from " + ", ".join(CATALOG) + " or all")
    return names


def configure_cache_env() -> None:
    """Keep Supertonic's files next to the Hugging Face cache, so one mounted folder holds every model."""
    if os.environ.get("HF_HOME") and not os.environ.get("SUPERTONIC_CACHE_DIR"):
        os.environ["SUPERTONIC_CACHE_DIR"] = str(Path(os.environ["HF_HOME"]) / "supertonic3")


def package_installed(model: str) -> bool:
    return importlib.util.find_spec(CATALOG[model].package) is not None


def _hf_files(model: str, precision: str) -> list[tuple[str, str]]:
    if model == "nano":
        return [(NANO_REPO, f) for f in NANO_FILES]
    folder = TURBO_DIRS.get(precision, "onnx_update")
    return [(TURBO_REPO, f"{folder}/{f}") for f in TURBO_FILES] + [(MOSS_REPO, f) for f in MOSS_FILES]


def _english_dir() -> Path | None:
    try:
        from supertonic.loader import get_cache_dir
    except ImportError:
        return None
    return Path(get_cache_dir("supertonic-3"))


def downloaded(model: str, precision: str = "fp32") -> bool:
    """Are all the files this model needs already on this machine?"""
    if model not in CATALOG:
        raise ValueError(f"unknown model {model!r}")
    if model == "english":
        folder = _english_dir()
        if folder is None:
            return False
        from supertonic.loader import has_all_onnx_modules

        return has_all_onnx_modules(folder / "onnx") or has_all_onnx_modules(folder)
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    return all(isinstance(try_to_load_from_cache(repo, name), str) for repo, name in _hf_files(model, precision))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def bytes_on_disk(model: str, precision: str = "fp32") -> int:
    """How much of the model has arrived so far, including a download still in progress."""
    if model == "english":
        folder = _english_dir()
        if folder is None:
            return 0
        total = 0
        for root in (folder, folder.parent / f".{folder.name}.tmp"):
            if root.is_dir():
                total += sum(_size(p) for p in root.rglob("*") if p.is_file() and ".cache" not in p.relative_to(root).parts)
        return total
    try:
        from huggingface_hub.constants import HF_HUB_CACHE
    except ImportError:
        return 0
    total = 0
    for repo in {r for r, _ in _hf_files(model, precision)}:
        blobs = Path(HF_HUB_CACHE) / ("models--" + repo.replace("/", "--")) / "blobs"
        if blobs.is_dir():
            total += sum(_size(p) for p in blobs.iterdir() if p.is_file())
    return total


def describe(model: str, precision: str = "fp32") -> dict:
    info = CATALOG[model]
    return {"id": info.id, "label": info.label, "language": info.language, "description": info.description,
            "size_mb": info.size_mb, "accelerators": list(info.accelerators)}
