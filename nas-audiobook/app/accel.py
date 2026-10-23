"""Which hardware runs the speech models: the CPU (the default) or a GPU.

The model libraries create their ONNX Runtime sessions themselves and mostly ask for the CPU only, so choosing a GPU
means changing the providers they ask for. `configure()` picks the machine's accelerator once at start. Each model
then decides for itself: `load_checked()` gives a model the accelerator only when the model can use it (the catalogue
says which devices each model supports), so a machine with a GPU still runs a CPU-only model on the CPU. `status()` says
what was asked for and what each model really got, so a GPU that is silently not in use shows up.

Turbo is the exception to "ONNX Runtime does everything": VieNeu runs it on the GPU through PyTorch with CUDA (its
own batched, CUDA-graph path), and only on the CPU through ONNX. So a machine whose PyTorch can use CUDA runs Turbo
there, whatever ONNX Runtime package it has, and `torch_cuda()` says whether that is the case.

A GPU also needs the matching ONNX Runtime package: `onnxruntime-gpu` (NVIDIA CUDA) or `onnxruntime-directml`
(any GPU on Windows). The ordinary `onnxruntime` package can only use the CPU (and CoreML on a Mac)."""
from __future__ import annotations

import logging
import importlib.util
import struct
import subprocess
import sys
import threading
from typing import Sequence

log = logging.getLogger("accel")

CPU = "CPUExecutionProvider"
TORCH_CUDA = "PyTorchCUDA"  # stands in for a provider name when a model runs on the GPU through PyTorch
DEVICES = {
    "cpu": [],
    "cuda": ["CUDAExecutionProvider"],           # NVIDIA, with onnxruntime-gpu
    "directml": ["DmlExecutionProvider"],        # any GPU on Windows, with onnxruntime-directml
    "coreml": ["CoreMLExecutionProvider"],       # Apple silicon, in the ordinary macOS package
    "openvino": ["OpenVINOExecutionProvider"],   # Intel, with onnxruntime-openvino
}
AUTO_ORDER = ["cuda", "directml"]  # `auto` never picks coreml or openvino: they are explicit choices
PACKAGES = {"cuda": "onnxruntime-gpu", "directml": "onnxruntime-directml", "coreml": "onnxruntime (macOS)",
            "openvino": "onnxruntime-openvino"}


class DeviceError(ValueError):
    pass


_lock = threading.Lock()
_state: dict = {"requested": "cpu", "device": "cpu", "wanted": [], "active": set(), "fallback": False,
                "patched": False, "models": {}, "torch": False}
_local = threading.local()  # while a model loads: the providers it may use, and the ones its sessions really got


def device_of(provider: str | None) -> str:
    """The device name (`cuda`, `directml`, ...) that a provider belongs to; `cpu` for the CPU or anything unknown."""
    if provider == TORCH_CUDA:
        return "cuda"
    return next((name for name, providers in DEVICES.items() if provider in providers), "cpu")


def parse_device(raw: str | None) -> str:
    value = (raw or "").strip().lower() or "cpu"
    if value != "auto" and value not in DEVICES:
        raise DeviceError(f"unknown device {raw!r}; choose auto, {', '.join(DEVICES)}")
    return value


def providers_for(device: str, available: list[str]) -> list[str]:
    """The providers to ask for, best first, ending in the CPU. Raises if a device was asked for by name and this
    ONNX Runtime cannot use it, because carrying on silently on the CPU is exactly what a flag is there to prevent."""
    device = parse_device(device)
    if device == "cpu":
        return []
    if device == "auto":
        for name in AUTO_ORDER:
            if all(p in available for p in DEVICES[name]):
                return list(DEVICES[name])
        return []
    missing = [p for p in DEVICES[device] if p not in available]
    if missing:
        raise DeviceError(f"device {device!r} needs {missing[0]}, which this ONNX Runtime does not offer "
                          f"(it offers {', '.join(available)}). Install {PACKAGES[device]} instead of onnxruntime, "
                          "and on a container give it access to the GPU")
    return list(DEVICES[device])


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        byte, n = n & 0x7F, n >> 7
        out.append(byte | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _field(number: int, wire: int, payload: bytes) -> bytes:
    return _varint(number << 3 | wire) + payload


def _bytes(number: int, data: bytes) -> bytes:
    return _field(number, 2, _varint(len(data)) + data)


def tiny_model() -> bytes:
    """The smallest model that needs cuDNN, written by hand: y = Conv(x, w) with a 1x1 kernel of weight 1 on a float
    tensor of shape [1, 1, 4], so it returns its input. A model without a convolution would pass on a machine that is
    missing cuDNN and then fail on the first real model, which is what the start-up test is there to catch. It lets the
    hardware be tried without any real model, and without depending on the onnx package."""
    def value_info(name: str) -> bytes:
        dims = b"".join(_bytes(1, _field(1, 0, _varint(d))) for d in (1, 1, 4))      # dim_value of each dimension
        tensor = _field(1, 0, _varint(1)) + _bytes(2, dims)                            # elem_type float, shape [1, 1, 4]
        return _bytes(1, name.encode()) + _bytes(2, _bytes(1, tensor))
    weight = (b"".join(_field(1, 0, _varint(d)) for d in (1, 1, 1)) + _field(2, 0, _varint(1))
              + _bytes(8, b"w") + _bytes(9, struct.pack("<f", 1.0)))                   # dims, float, name, raw_data
    node = _bytes(1, b"x") + _bytes(1, b"w") + _bytes(2, b"y") + _bytes(4, b"Conv")
    graph = (_bytes(1, node) + _bytes(2, b"tiny") + _bytes(5, weight) + _bytes(11, value_info("x"))
             + _bytes(12, value_info("y")))
    return _field(1, 0, _varint(8)) + _bytes(8, _field(2, 0, _varint(13))) + _bytes(7, graph)  # ir 8, opset 13


SELF_TEST = """
import sys, numpy as np, onnxruntime as ort
wanted = sys.argv[1]
if wanted == "CUDAExecutionProvider" and hasattr(ort, "preload_dlls"):
    try:
        ort.preload_dlls()  # find CUDA and cuDNN installed as pip packages (onnxruntime-gpu[cuda,cudnn])
    except Exception:
        pass
session = ort.InferenceSession(bytes.fromhex(sys.argv[2]), providers=[wanted, "CPUExecutionProvider"])
session.run(None, {"x": np.arange(4, dtype=np.float32).reshape(1, 1, 4)})
print("PROVIDER", session.get_providers()[0])
"""


TORCH_SELF_TEST = """
import torch
assert torch.cuda.is_available(), "torch.cuda.is_available() is False (this PyTorch has no CUDA, or no usable GPU)"
x = torch.ones(8, device="cuda")
print("TORCH", float((x * 2).sum()))
"""


def _torch_installed() -> bool:
    return importlib.util.find_spec("torch") is not None


def torch_cuda_test(timeout: float = 180.0, script: str = TORCH_SELF_TEST) -> tuple[bool, str]:
    """Can PyTorch run on a CUDA GPU here? Tried in a child process, like the ONNX Runtime test."""
    try:
        done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"it did not answer within {timeout:.0f} seconds"
    if done.returncode != 0:
        lines = [ln for ln in (done.stderr or done.stdout).strip().splitlines() if ln.strip()]
        return False, lines[-1][:240] if lines else f"the test process failed ({done.returncode})"
    return ("TORCH 16.0" in done.stdout), done.stdout.strip()[-80:]


def torch_cuda() -> bool:
    """Does this machine run Turbo on the GPU through PyTorch? Decided at start by `configure()`."""
    return bool(_state["torch"])


def self_test(provider: str, timeout: float = 180.0, script: str = SELF_TEST) -> tuple[bool, str]:
    """Try a provider on a tiny model in a separate process. A GPU that is missing or broken can crash the whole
    process rather than raise an error, and that must never happen in the middle of somebody's book. Returns
    (worked, what happened)."""
    try:
        done = subprocess.run([sys.executable, "-c", script, provider, tiny_model().hex()], capture_output=True,
                              text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"it did not answer within {timeout:.0f} seconds"
    if done.returncode < 0:
        return False, f"the test process crashed (signal {-done.returncode}), which usually means no working GPU driver"
    if done.returncode != 0:
        lines = [ln for ln in (done.stderr or done.stdout).strip().splitlines() if ln.strip()]
        return False, lines[-1][:240] if lines else f"the test process failed ({done.returncode})"
    got = next((ln.split(None, 1)[1] for ln in done.stdout.splitlines() if ln.startswith("PROVIDER ")), "?")
    if got != provider:
        return False, f"ONNX Runtime ran it on {got} instead (the GPU libraries, CUDA or cuDNN, are probably missing)"
    return True, got


def configure(device: str | None, ort=None, verify: bool = True) -> dict:
    """Apply the choice to every ONNX Runtime session created from now on. Returns `status()`. With `verify`, a GPU is
    first tried on a tiny model in a child process: an explicit choice that fails stops the start with the reason
    (`auto` quietly uses the CPU)."""
    requested = parse_device(device)
    torch_ok = False
    if requested in ("auto", "cuda") and verify and _torch_installed():
        torch_ok, detail = torch_cuda_test()
        if not torch_ok:
            (log.warning if requested == "cuda" else log.info)(
                "PyTorch cannot use CUDA here (%s), so Turbo will run on the CPU", detail)
    if ort is None:
        if requested == "cpu":
            with _lock:
                _state.update(requested="cpu", device="cpu", wanted=[], active=set(), fallback=False, models={},
                              torch=False)
            return status()
        import onnxruntime as ort  # noqa: PLC0415
    try:
        wanted = providers_for(requested, list(ort.get_available_providers()))
    except DeviceError:
        if not torch_ok:  # an explicit device that nothing here can provide is an error; PyTorch may still serve Turbo
            raise
        wanted = []
    tried = False
    if wanted and verify:
        worked, detail = self_test(wanted[0])
        if not worked and requested != "auto":
            raise DeviceError(f"device {requested!r} was requested but {wanted[0]} could not start: {detail}")
        if not worked:
            log.warning("device auto: %s could not start (%s); using the CPU", wanted[0], detail)
            wanted, tried = [], True
    if wanted and wanted[0] == "CUDAExecutionProvider" and hasattr(ort, "preload_dlls"):
        try:
            if torch_ok:
                import torch  # noqa: F401, PLC0415: load PyTorch's own cuDNN first; the preload would load another version
            ort.preload_dlls()
        except Exception as exc:  # noqa: BLE001: the GPU check below says whether it matters
            log.warning("could not preload the CUDA libraries: %s", exc)
    with _lock:
        _state.update(requested=requested, device=device_of(wanted[0]) if wanted else "cpu", wanted=wanted,
                      active=set(), fallback=False, models={}, torch=torch_ok)
        if wanted and not _state["patched"]:
            _patch(ort)
            _state["patched"] = True
    if torch_ok:
        log.info("device %s: Turbo will run on the GPU through PyTorch", requested)
    if requested == "auto" and not wanted and not tried:
        log.info("device auto: no GPU provider is available, using the CPU")
    elif wanted:
        log.info("device %s: models will run on %s (CPU as the fallback)", requested, ", ".join(wanted))
    return status()


def _patch(ort) -> None:
    original = ort.InferenceSession

    class Session(original):
        def __init__(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kwargs):
            override = getattr(_local, "wanted", None)  # set while a model loads, to what that model may use
            wanted = list(_state["wanted"] if override is None else override)
            asked = list(providers or [])
            if wanted and asked in ([], [CPU]):  # a library that only asked for the CPU, or nothing
                providers, provider_options = [*wanted, CPU], None
            super().__init__(path_or_bytes, sess_options, providers=providers, provider_options=provider_options, **kwargs)
            seen = getattr(_local, "seen", None)
            if seen is not None:
                seen.append((self.get_providers() or [CPU])[0])
            if wanted and providers and providers[0] == wanted[0]:
                _record(self.get_providers(), wanted[0])

    ort.InferenceSession = Session


def _load_with(load, wanted: list[str]):
    """Run `load` with only `wanted` offered to the sessions it creates. Returns the handle and the first provider each
    of those sessions really got."""
    _local.wanted, _local.seen = wanted, []
    try:
        return load(), list(_local.seen)
    finally:
        _local.wanted = _local.seen = None


def _note(model: str | None, provider: str, fallback: bool, note: str | None = None) -> None:
    if model is not None:
        with _lock:
            _state["models"][model] = {"device": device_of(provider), "provider": provider, "fallback": fallback,
                                       "note": note}
        log.info("the %s model runs on %s%s", model, device_of(provider).upper() if device_of(provider) != "cpu" else "the CPU",
                 f" ({note})" if note else "")


def device_for(accelerators: Sequence[str] | None) -> str:
    """The device a model will be tried on: this machine's accelerator if the model supports it, else the CPU."""
    device = _state["device"]
    return device if _state["wanted"] and (accelerators is None or device in accelerators) else "cpu"


def record(model: str, provider: str, fallback: bool = False, note: str | None = None) -> None:
    """Say where a model that was loaded outside `load_checked` (Turbo on PyTorch) runs."""
    _note(model, provider, fallback, note)


def load_checked(load, check, model: str | None = None, accelerators: Sequence[str] | None = None,
                 cpu_note: str | None = None, cpu_fallback: bool = False):
    """Load a model on the device that suits it. The machine's accelerator is offered only to a model that supports it
    (`accelerators`; None means any), so a CPU-only model never even tries the GPU. When the accelerator is used, prove
    it works by running `check` on the model: a GPU provider can accept a graph at load time and then fail when it runs
    ("error in building plan"), so a model that fails the check is loaded again on the CPU instead of failing every
    request. What each model got, and any fallback, is reported in `status()`. `cpu_note` replaces the usual note for a
    model that runs on the CPU ("" for none), and `cpu_fallback` marks that as a fallback."""
    if device_for(accelerators) == "cpu":
        handle, _ = _load_with(load, [])
        if cpu_note is None:
            cpu_note = (f"the {model} model runs on the CPU only (it cannot use {_state['device']})"
                        if _state["wanted"] else None)
        if cpu_fallback:
            with _lock:
                _state["fallback"] = True
        _note(model, CPU, cpu_fallback, cpu_note or None)
        return handle
    wanted = list(_state["wanted"])
    handle, seen = _load_with(load, wanted)
    try:
        check(handle)
        got = seen[0] if seen else CPU
        _note(model, got, got != wanted[0], None if got == wanted[0] else f"asked for {_state['device']} but got the CPU")
        return handle
    except Exception as exc:  # noqa: BLE001: any failure of the model on this hardware means "use the CPU"
        log.warning("a model failed its first run on %s (%s: %s); loading it on the CPU instead",
                    ", ".join(wanted), type(exc).__name__, str(exc)[:160])
    with _lock:
        _state["fallback"] = True
    _note(model, CPU, True, f"it failed its first run on {_state['device']}, so it runs on the CPU")
    handle, _ = _load_with(load, [])
    return handle


def _record(got: list[str], wanted: str) -> None:
    first = got[0] if got else CPU
    with _lock:
        _state["active"].add(first)
        if first != wanted:
            _state["fallback"] = True
    if first != wanted:
        log.warning("a model asked for %s but got %s: the GPU libraries may be missing (CUDA/cuDNN) or this model "
                    "step is not supported there", wanted, first)


def status() -> dict:
    """What was asked for, the accelerator this machine picked, which providers the sessions created so far actually
    use, whether any fell back, and per model the device it runs on."""
    with _lock:
        wanted = list(_state["wanted"])
        models = {m: dict(e) for m, e in _state["models"].items()}
        active = sorted({e["provider"] for e in models.values()} or _state["active"])
        return {"requested": _state["requested"], "device": _state["device"], "providers": wanted or [CPU],
                "active": active or None, "using_gpu": any(p != CPU for p in active),
                "torch_cuda": bool(_state["torch"]), "fallback": _state["fallback"], "models": {m: {k: v for k, v in e.items() if k != "provider"} for m, e in models.items()}}
