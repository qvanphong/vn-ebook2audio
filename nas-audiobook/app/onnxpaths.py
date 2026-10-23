"""Let newer ONNX Runtime load models from the Hugging Face cache.

The cache stores each file once under `blobs/<xx>/<hash>` and lists it in `snapshots/` as a symlink. A model whose
weights live in a separate "external data" file (the Turbo model) then has its `.onnx` file and its `.data` file in
different blob folders. Newer ONNX Runtime versions check that external data stays inside the model's own folder and
refuse with "External data path escapes model directory". Older ones (1.22) do not check.

`install()` wraps `InferenceSession` so that, for such a model, a real folder is made holding links to the model and to
the data file under their proper names, and the session is created from there. Nothing else is touched: a model given
as bytes, a model without external data, or one whose data is already beside it loads exactly as before."""
from __future__ import annotations

import hashlib
import logging
import mmap
import os
import shutil
import threading
from pathlib import Path

log = logging.getLogger("onnxpaths")
_lock = threading.Lock()
_installed = False


def cache_dir() -> Path:
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "onnx-real"


def external_files(model: Path) -> list[Path]:
    """Files next to `model` that its own bytes name, which is how ONNX stores the location of external data."""
    siblings = [p for p in model.parent.iterdir() if p != model and p.is_file() and len(p.name) > 3]
    if not siblings or model.stat().st_size == 0:
        return []
    with model.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        return [p for p in siblings if data.find(p.name.encode()) != -1]


def escapes(model: Path, data: list[Path]) -> bool:
    here = Path(os.path.realpath(model)).parent
    return any(Path(os.path.realpath(p)).parent != here for p in data)


def _link(real: Path, target: Path) -> None:
    if target.exists() and target.stat().st_size == real.stat().st_size:
        return
    target.unlink(missing_ok=True)
    try:
        os.link(real, target)  # no extra disk space; both are the same file
    except OSError:
        shutil.copy2(real, target)  # another disk, or a file system without hard links


def materialize(model: Path, data: list[Path], root: Path | None = None) -> Path:
    """A folder where the model and its data files sit together as real files, and the model's path inside it."""
    key = hashlib.sha1(os.path.realpath(model).encode()).hexdigest()[:12]
    folder = (root or cache_dir()) / key
    folder.mkdir(parents=True, exist_ok=True)
    for source in (model, *data):
        _link(Path(os.path.realpath(source)), folder / source.name)
    return folder / model.name


def resolve(path_or_bytes, root: Path | None = None):
    """The path to give ONNX Runtime: the same one, unless its external data would escape the model's folder."""
    if not isinstance(path_or_bytes, (str, os.PathLike)):
        return path_or_bytes
    model = Path(path_or_bytes)
    try:
        if model.suffix != ".onnx" or not model.is_file():
            return path_or_bytes
        data = external_files(model)
        if not data or not escapes(model, data):
            return path_or_bytes
        fixed = materialize(model, data, root)
    except OSError as exc:  # leave it to ONNX Runtime to report whatever is really wrong
        log.debug("not adjusting %s: %s", path_or_bytes, exc)
        return path_or_bytes
    log.info("%s keeps its weights in another folder of the Hugging Face cache; loading it from %s", model.name, fixed.parent)
    return str(fixed)


def install(ort=None) -> bool:
    """Wrap `InferenceSession` once. Returns False when ONNX Runtime is not installed (a server that only uses workers)."""
    global _installed
    with _lock:
        if _installed:
            return True
        if ort is None:
            try:
                import onnxruntime as ort  # noqa: PLC0415
            except ImportError:
                return False
        original = ort.InferenceSession

        class Session(original):
            def __init__(self, path_or_bytes, *args, **kwargs):
                super().__init__(resolve(path_or_bytes), *args, **kwargs)

        ort.InferenceSession = Session
        _installed = True
        return True
