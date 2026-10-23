import threading

import pytest

from app import tts
from app.tts import LocalBackend, ModelLoading, ModelPool


class SlowPool(ModelPool):
    def __init__(self, fail=False, voice_cache=None, clones=None):
        super().__init__(LocalBackend(1, "fp32"), clones, voice_cache)
        self.gate, self.fail = threading.Event(), fail

    def get(self, model):
        self.gate.wait(5)
        if self.fail:
            raise OSError("no network")
        self._models[model] = type("M", (), {"list_preset_voices": lambda s: [("Label", "id")]})()
        self._remember_presets(model, self._models[model])
        return self._models[model]


def test_voices_raise_loading_immediately_then_succeed_when_loaded():
    pool = SlowPool()
    with pytest.raises(ModelLoading) as first:
        pool.voices("nano")
    assert first.value.error is None
    pool.gate.set()
    for _ in range(100):
        if pool.ready("nano"):
            break
        threading.Event().wait(0.02)
    assert pool.voices("nano").voices == [{"id": "id", "label": "Label", "cloned": False}]


def test_load_failure_is_reported_and_retried_on_next_request():
    pool = SlowPool(fail=True)
    pool.gate.set()
    with pytest.raises(ModelLoading):
        pool.voices("nano")
    for _ in range(100):
        if "nano" in pool._errors:
            break
        threading.Event().wait(0.02)
    with pytest.raises(ModelLoading) as again:
        pool.voices("nano")
    assert again.value.error is None or "no network" in again.value.error
    pool.fail = False
    for _ in range(100):
        try:
            pool.voices("nano")
            break
        except ModelLoading:
            threading.Event().wait(0.02)
    assert pool.ready("nano")


def test_unknown_model_is_a_value_error_not_a_load():
    with pytest.raises(ValueError):
        ModelPool.local(1, "fp32").voices("huge")


def wait_until(predicate, seconds=3):
    for _ in range(int(seconds / 0.02)):
        if predicate():
            return
        threading.Event().wait(0.02)
    raise AssertionError("timed out")


def test_voices_come_from_the_disk_cache_while_the_model_loads(tmp_path):
    cache = tmp_path / "voices.json"
    first = SlowPool(voice_cache=cache)
    with pytest.raises(ModelLoading):
        first.voices("nano")
    first.gate.set()
    wait_until(lambda: cache.exists() and first.ready("nano"))

    restarted = SlowPool(voice_cache=cache)
    shown = restarted.voices("nano")
    assert shown.ready is False and [v["id"] for v in shown.voices] == ["id"]
    restarted.gate.set()
    wait_until(lambda: restarted.ready("nano"))
    assert restarted.voices("nano").ready is True


def test_cached_voices_are_per_model_and_clones_show_without_the_model(tmp_path):
    from app.tts import CloneStore

    clones = CloneStore(tmp_path / "clones")
    wav = tmp_path / "a.wav"; wav.write_bytes(b"RIFF")
    clones.add("Giọng tôi", wav)
    cache = tmp_path / "voices.json"
    cache.write_text('{"nano": [{"id": "A", "label": "A", "cloned": false}]}', encoding="utf8")
    pool = SlowPool(voice_cache=cache, clones=clones)
    shown = pool.voices("nano")
    assert [(v["id"], v["cloned"]) for v in shown.voices] == [("A", False), ("Giọng tôi", True)]
    with pytest.raises(ModelLoading):
        pool.voices("turbo")


def test_a_failed_load_is_reported_next_to_the_cached_list(tmp_path):
    cache = tmp_path / "voices.json"
    cache.write_text('{"nano": [{"id": "A", "label": "A", "cloned": false}]}', encoding="utf8")
    pool = SlowPool(fail=True, voice_cache=cache)
    pool.gate.set()
    pool.voices("nano")
    wait_until(lambda: "nano" in pool._errors)
    shown = pool.voices("nano")
    assert shown.voices and shown.ready is False


def test_voices_answer_immediately_even_while_the_real_loader_holds_the_model_open(tmp_path, monkeypatch):
    import sys
    import time
    import types

    release = threading.Event()

    class SlowVieneu:
        def __init__(self, mode, **kwargs):
            release.wait(10)

        def list_preset_voices(self):
            return [("Label", "id")]

    monkeypatch.setitem(sys.modules, "vieneu", types.SimpleNamespace(Vieneu=SlowVieneu))
    cache = tmp_path / "voices.json"
    cache.write_text('{"nano": [{"id": "A", "label": "A", "cloned": false}]}', encoding="utf8")
    pool = ModelPool.local(1, "fp32", None, cache)
    pool.voices("nano")
    time.sleep(0.1)

    started = time.monotonic()
    shown = pool.voices("nano")
    assert time.monotonic() - started < 0.5, "voices() waited for the model to finish loading"
    assert shown.ready is False and shown.voices[0]["id"] == "A"
    release.set()
    wait_until(lambda: pool.ready("nano"))
    assert pool.voices("nano").voices[0]["id"] == "id"
