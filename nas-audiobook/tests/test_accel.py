"""Choosing the hardware (CPU or GPU) the speech models run on."""
import logging
import types

import pytest

from app import accel
from app.accel import DeviceError, configure, parse_device, providers_for, status
from app.config import Settings

CPU = "CPUExecutionProvider"
CUDA = "CUDAExecutionProvider"


def fake_ort(available, gpu_works=True):
    """A stand-in for the onnxruntime module: sessions record what they were asked for."""
    created = []

    class InferenceSession:
        def __init__(self, path_or_bytes, sess_options=None, providers=None, provider_options=None, **kw):
            self.path, self.asked, self.options = path_or_bytes, providers, provider_options
            self.granted = [p for p in (providers or [CPU]) if p == CPU or gpu_works] or [CPU]
            created.append(self)

        def get_providers(self):
            return self.granted

    return types.SimpleNamespace(InferenceSession=InferenceSession, get_available_providers=lambda: available,
                                 created=created)


@pytest.fixture(autouse=True)
def clean_state():
    accel._state.update(requested="cpu", wanted=[], active=set(), fallback=False, patched=False)
    yield
    accel._state.update(requested="cpu", wanted=[], active=set(), fallback=False, patched=False)


def test_the_default_is_the_cpu_and_changes_nothing():
    ort = fake_ort([CUDA, CPU])
    original = ort.InferenceSession
    assert configure(None, ort, verify=False)["requested"] == "cpu" and ort.InferenceSession is original
    assert status() == {"requested": "cpu", "providers": [CPU], "active": None, "using_gpu": False, "fallback": False}


def test_names_are_case_insensitive_and_unknown_ones_are_refused_with_the_choices():
    assert parse_device(" CUDA ") == "cuda" and parse_device("") == "cpu" and parse_device(None) == "cpu"
    with pytest.raises(DeviceError, match="unknown device 'gpu'.*auto, cpu, cuda"):
        parse_device("gpu")


def test_asking_for_cuda_without_it_is_an_error_that_says_what_to_install():
    with pytest.raises(DeviceError, match="needs CUDAExecutionProvider.*onnxruntime-gpu"):
        providers_for("cuda", [CPU])
    with pytest.raises(DeviceError, match="onnxruntime-directml"):
        providers_for("directml", [CPU, "CoreMLExecutionProvider"])


def test_auto_takes_a_gpu_when_there_is_one_and_the_cpu_otherwise():
    assert providers_for("auto", [CUDA, CPU]) == [CUDA]
    assert providers_for("auto", ["DmlExecutionProvider", CPU]) == ["DmlExecutionProvider"]
    assert providers_for("auto", [CPU]) == []
    assert providers_for("auto", ["CoreMLExecutionProvider", CPU]) == []  # coreml is only ever an explicit choice
    assert providers_for("coreml", ["CoreMLExecutionProvider", CPU]) == ["CoreMLExecutionProvider"]


def test_a_library_that_asks_only_for_the_cpu_gets_the_gpu_first_with_the_cpu_as_backup():
    ort = fake_ort([CUDA, CPU])
    configure("cuda", ort, verify=False)
    ort.InferenceSession("a.onnx", providers=[CPU])
    ort.InferenceSession("b.onnx")
    ort.InferenceSession("c.onnx", None, [CPU], [{}])
    assert [s.asked for s in ort.created] == [[CUDA, CPU]] * 3
    assert ort.created[2].options is None
    s = status()
    assert s["requested"] == "cuda" and s["active"] == [CUDA] and s["using_gpu"] is True and s["fallback"] is False


def test_a_provider_list_a_library_chose_itself_is_left_alone():
    ort = fake_ort([CUDA, CPU])
    configure("cuda", ort, verify=False)
    ort.InferenceSession("x.onnx", providers=["SomethingElseProvider", CPU])
    assert ort.created[0].asked == ["SomethingElseProvider", CPU]


def test_a_gpu_that_could_not_be_started_is_reported_not_hidden(caplog):
    ort = fake_ort([CUDA, CPU], gpu_works=False)  # ONNX Runtime fell back to the CPU, as it does when cuDNN is missing
    configure("cuda", ort, verify=False)
    with caplog.at_level(logging.WARNING, logger="accel"):
        ort.InferenceSession("a.onnx", providers=[CPU])
    s = status()
    assert s["fallback"] is True and s["using_gpu"] is False and s["active"] == [CPU]
    assert "asked for CUDAExecutionProvider but got CPUExecutionProvider" in caplog.text


def test_configuring_twice_does_not_stack_wrappers_and_resets_what_was_seen():
    ort = fake_ort([CUDA, CPU])
    configure("cuda", ort, verify=False)
    first = ort.InferenceSession
    ort.InferenceSession("a.onnx")
    configure("cuda", ort, verify=False)
    assert ort.InferenceSession is first and status()["active"] is None
    ort.InferenceSession("b.onnx")
    assert ort.created[-1].asked == [CUDA, CPU]


def test_a_cpu_choice_after_a_gpu_choice_stops_rewriting():
    ort = fake_ort([CUDA, CPU])
    configure("cuda", ort, verify=False)
    configure("cpu", ort, verify=False)
    ort.InferenceSession("a.onnx", providers=[CPU])
    assert ort.created[-1].asked == [CPU]


def test_the_setting_comes_from_the_environment(tmp_path, monkeypatch):
    assert Settings(data_dir=tmp_path).device == "cpu"
    monkeypatch.setenv("TTS_DEVICE", " CUDA ")
    assert Settings.from_env().device == "cuda"
    monkeypatch.setenv("TTS_DEVICE", "")
    assert Settings.from_env().device == "cpu"


def test_the_worker_reports_its_device_so_the_server_can_show_it(tmp_path):
    from test_worker import auth, make_worker

    c, _, _ = make_worker(tmp_path)
    device = c.get("/health", headers=auth()).json()["device"]
    assert device["requested"] == "cpu" and device["using_gpu"] is False and device["fallback"] is False


def test_the_server_status_carries_each_machines_device(settings, monkeypatch):
    import httpx
    from app.engine_config import EngineConfig, EngineConfigStore, EngineController
    from app.tts import ModelPool
    from conftest import FakeBackend

    ctl = EngineController(EngineConfigStore(settings.engine_config), EngineConfig(),
                           lambda w: ModelPool(FakeBackend(), None, settings.voice_cache))
    ctl.apply(True, [{"url": "10.0.0.5:8780", "name": "Gaming PC"}])
    gpu = {"requested": "cuda", "providers": [CUDA], "active": [CUDA], "using_gpu": True, "fallback": False}

    def get(url, headers=None, timeout=None):
        body = {"ok": True, "protocol": 3, "models": {}, "device": gpu} if url.endswith("/health") else {"models": {}}
        return httpx.Response(200, json=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    rows = {r["name"]: r for r in ctl.status()}
    assert rows["Gaming PC"]["device"] == gpu and rows["This server"]["device"]["requested"] == "cpu"


# --- a model that fails on the GPU is loaded again on the CPU --------------------------------

def test_a_model_that_runs_fine_on_the_gpu_is_loaded_once():
    ort = fake_ort([CUDA, CPU])
    configure("cuda", ort, verify=False)
    loads, checks = [], []
    handle = accel.load_checked(lambda: loads.append(1) or "handle", lambda h: checks.append(h))
    assert handle == "handle" and loads == [1] and checks == ["handle"] and status()["fallback"] is False


def test_on_the_cpu_nothing_is_checked_and_nothing_is_loaded_twice():
    loads = []
    assert accel.load_checked(lambda: loads.append(1) or "h", lambda h: pytest.fail("no check needed")) == "h"
    assert loads == [1]


def test_a_model_that_fails_its_first_run_on_the_gpu_is_reloaded_on_the_cpu_and_the_fallback_is_reported(caplog):
    ort = fake_ort([CUDA, CPU])
    configure("cuda", ort, verify=False)
    sessions = []

    def load():
        sessions.append(ort.InferenceSession("m.onnx", providers=[CPU]))
        return sessions[-1]

    def check(handle):
        if CUDA in handle.asked:
            raise RuntimeError("Error in building plan")

    with caplog.at_level(logging.WARNING, logger="accel"):
        handle = accel.load_checked(load, check)
    assert [s.asked for s in sessions] == [[CUDA, CPU], [CPU]] and handle is sessions[1]
    s = status()
    assert s["fallback"] is True and s["using_gpu"] is False
    assert "loading it on the CPU instead" in caplog.text and "Error in building plan" in caplog.text
    ort.InferenceSession("later.onnx", providers=[CPU])  # the GPU is switched back on for the next model
    assert ort.created[-1].asked == [CUDA, CPU]


def test_the_local_backend_proves_each_model_with_a_short_first_run(monkeypatch):
    from app import tts

    ort = fake_ort([CUDA, CPU])
    configure("cuda", ort, verify=False)
    ran = []

    class Handle:
        def infer(self, text, **kw):
            ran.append(text)

    monkeypatch.setattr(tts.LocalBackend, "_load", lambda self, model: Handle())
    tts.LocalBackend(1, "fp32").load("english")
    tts.LocalBackend(1, "fp32").load("nano")
    assert ran == ["Hello.", "Xin chào."]


# --- trying the hardware before relying on it ---------------------------------------------------

def test_the_hand_written_model_is_a_valid_onnx_model_that_returns_its_input():
    ort = pytest.importorskip("onnxruntime")
    import numpy as np

    session = ort.InferenceSession(accel.tiny_model(), providers=["CPUExecutionProvider"])
    out = session.run(None, {"x": np.arange(4, dtype=np.float32)})[0]
    assert out.tolist() == [0.0, 1.0, 2.0, 3.0]


def test_the_self_test_passes_for_a_provider_that_works():
    pytest.importorskip("onnxruntime")
    assert accel.self_test(CPU) == (True, CPU)


def test_a_provider_the_runtime_does_not_have_is_reported_by_the_self_test():
    pytest.importorskip("onnxruntime")
    worked, detail = accel.self_test("NoSuchExecutionProvider")
    assert worked is False and detail


def test_a_process_that_crashes_is_reported_as_a_missing_or_broken_gpu_not_a_dead_worker():
    worked, detail = accel.self_test(CUDA, script="import os, signal; os.kill(os.getpid(), signal.SIGSEGV)")
    assert worked is False and "crashed (signal 11)" in detail and "driver" in detail


def test_a_process_that_hangs_is_given_up_on():
    worked, detail = accel.self_test(CUDA, timeout=0.5, script="import time; time.sleep(30)")
    assert worked is False and "did not answer" in detail


def test_a_provider_that_silently_gave_the_cpu_is_not_a_pass():
    script = "print('PROVIDER CPUExecutionProvider')"
    worked, detail = accel.self_test(CUDA, script=script)
    assert worked is False and "ran it on CPUExecutionProvider instead" in detail


def test_an_explicit_gpu_that_fails_its_test_stops_the_start_with_the_reason(monkeypatch):
    ort = fake_ort([CUDA, CPU])
    monkeypatch.setattr(accel, "self_test", lambda provider: (False, "the test process crashed (signal 11)"))
    with pytest.raises(DeviceError, match="'cuda' was requested but CUDAExecutionProvider could not start: .*signal 11"):
        configure("cuda", ort)
    assert ort.InferenceSession.__name__ == "InferenceSession"  # nothing was patched


def test_auto_uses_the_cpu_when_the_gpu_fails_its_test(monkeypatch, caplog):
    ort = fake_ort([CUDA, CPU])
    monkeypatch.setattr(accel, "self_test", lambda provider: (False, "no driver"))
    with caplog.at_level(logging.WARNING, logger="accel"):
        result = configure("auto", ort)
    assert result["providers"] == [CPU] and "using the CPU" in caplog.text
    ort.InferenceSession("m.onnx", providers=[CPU])
    assert ort.created[-1].asked == [CPU]


def test_a_gpu_that_passes_its_test_is_used(monkeypatch):
    ort = fake_ort([CUDA, CPU])
    monkeypatch.setattr(accel, "self_test", lambda provider: (True, provider))
    assert configure("cuda", ort)["providers"] == [CUDA]
    ort.InferenceSession("m.onnx", providers=[CPU])
    assert ort.created[-1].asked == [CUDA, CPU]
