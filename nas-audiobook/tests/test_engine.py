import json
import os
import stat

import httpx
import pytest
from fastapi.testclient import TestClient

from app import engine_config
from app.api import create_app
from app.engine_config import (ConfigError, EngineConfig, EngineConfigStore, EngineController, default_config,
                               normalize_url)
from app.fleet import LOCAL_ID, WorkerConfig
from app.model import Voice
from app.remote import RemoteBackend
from app.tts import ModelPool
from conftest import FakeBackend
from test_worker import TOKEN, make_worker

ONE = WorkerConfig("w1", "Pixel", "http://10.0.0.5:8780", "tok", True)


def controller(settings, default=EngineConfig(), make_pool=None):
    made = []

    def default_pool(worker):
        pool = ModelPool(FakeBackend(), None, settings.voice_cache)
        made.append((worker, pool))
        return pool

    ctl = EngineController(EngineConfigStore(settings.engine_config), default, make_pool or default_pool)
    ctl.made = made
    return ctl


def worker_entry(url, **kw):
    return {"url": url, **kw}


@pytest.fixture(autouse=True)
def models_installed(monkeypatch):
    monkeypatch.setattr(engine_config, "local_available", lambda: True)


def test_urls_are_normalised_and_bad_ones_rejected():
    assert normalize_url("192.168.1.20:8780/") == "http://192.168.1.20:8780"
    assert normalize_url(" https://phone.local ") == "https://phone.local"
    for bad in ("", "ftp://x", "http://", "   "):
        with pytest.raises(ConfigError):
            normalize_url(bad)


def test_config_is_saved_privately_and_defaults_apply_without_a_file(tmp_path):
    store = EngineConfigStore(tmp_path / "config" / "engine.json")
    default = EngineConfig(False, (ONE,))
    assert store.load(default) == default
    other = EngineConfig(True, (WorkerConfig("w2", "Laptop", "http://b:2", "secret", False),))
    store.save(other)
    assert store.load(default) == other
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600


def test_the_single_worker_file_of_earlier_versions_still_loads(tmp_path):
    path = tmp_path / "engine.json"
    path.write_text(json.dumps({"backend": "remote", "worker_url": "http://old:1", "worker_token": "t"}))
    loaded = EngineConfigStore(path).load(EngineConfig())
    assert loaded.local_enabled is False
    assert [(w.url, w.token, w.enabled) for w in loaded.workers] == [("http://old:1", "t", True)]
    path.write_text(json.dumps({"backend": "local"}))
    assert EngineConfigStore(path).load(EngineConfig(False)) == EngineConfig(True, ())


def test_environment_defaults(monkeypatch):
    assert default_config("local", None, None) == EngineConfig(True, ())
    remote = default_config("remote", "10.0.0.9:8780", "t")
    assert remote.local_enabled is False and remote.workers[0].url == "http://10.0.0.9:8780"
    with pytest.raises(ConfigError, match="TTS_WORKER_URL"):
        default_config("remote", None, None)
    with pytest.raises(ConfigError, match="local or remote"):
        default_config("cloud", None, None)


def test_workers_get_ids_names_and_a_token_that_is_never_revealed(settings):
    ctl = controller(settings)
    view = ctl.apply(True, [worker_entry("10.0.0.5:8780", name="Pixel", token="s3cr3t-value"),
                            worker_entry("laptop.local:8780")])
    assert [w["name"] for w in view["workers"]] == ["Pixel", "laptop.local"]
    assert all(w["id"] for w in view["workers"]) and view["workers"][0]["has_token"] is True
    assert "s3cr3t-value" not in json.dumps(view)
    assert json.loads(settings.engine_config.read_text())["workers"][0]["token"] == "s3cr3t-value"
    restarted = controller(settings)
    assert [w.url for w in restarted.config.workers] == ["http://10.0.0.5:8780", "http://laptop.local:8780"]


def test_a_blank_token_keeps_the_saved_one_and_clear_token_removes_it(settings):
    ctl = controller(settings)
    wid = ctl.apply(True, [worker_entry("10.0.0.5:8780", token="keep-me")])["workers"][0]["id"]
    ctl.apply(True, [worker_entry("10.0.0.5:8780", id=wid, token="")])
    assert ctl.config.workers[0].token == "keep-me"
    ctl.apply(True, [worker_entry("10.0.0.5:8780", id=wid, clear_token=True)])
    assert ctl.config.workers[0].token is None


def test_unchanged_workers_keep_their_loaded_models_and_changed_ones_start_fresh(settings):
    ctl = controller(settings)
    first = ctl.apply(True, [worker_entry("10.0.0.5:8780", token="t"), worker_entry("10.0.0.6:8780")])
    ids = [w["id"] for w in first["workers"]]
    before = {n.id: n.pool for n in ctl.nodes()}
    ctl.apply(True, [worker_entry("10.0.0.5:8780", id=ids[0], name="Renamed"), worker_entry("10.0.0.7:8780", id=ids[1])])
    after = {n.id: n.pool for n in ctl.nodes()}
    assert after[ids[0]] is before[ids[0]] and after[LOCAL_ID] is before[LOCAL_ID]
    assert after[ids[1]] is not before[ids[1]]
    assert ctl.nodes()[1].label == "Renamed"


def test_the_list_must_be_valid_and_leave_a_machine_switched_on(settings, monkeypatch):
    ctl = controller(settings)
    for bad in ("", "ftp://x", "nonsense://"):
        with pytest.raises(ConfigError):
            ctl.apply(True, [worker_entry(bad)])
    with pytest.raises(ConfigError, match="twice"):
        ctl.apply(True, [worker_entry("10.0.0.5:8780"), worker_entry("http://10.0.0.5:8780/")])
    with pytest.raises(ConfigError, match="at least one"):
        ctl.apply(False, [worker_entry("10.0.0.5:8780", enabled=False)])
    ctl.apply(False, [worker_entry("10.0.0.5:8780")])
    monkeypatch.setattr(engine_config, "local_available", lambda: False)
    with pytest.raises(ConfigError, match="cannot run them"):
        ctl.apply(True, [])
    assert ctl.view()["local"]["available"] is False


def test_conversions_use_the_machines_they_were_given_or_every_enabled_one(settings):
    ctl = controller(settings)
    ctl.apply(True, [worker_entry("10.0.0.5:8780", name="A"), worker_entry("10.0.0.6:8780", name="B", enabled=False)])
    a, b = (w.id for w in ctl.config.workers)
    assert [n.label for n in ctl.lanes(None)] == ["This server", "A"]
    assert [n.label for n in ctl.lanes([b, a, "gone", b])] == ["B", "A"]
    assert ctl.lanes(["gone"]) == []


def test_a_conversion_keeps_its_machines_when_the_list_changes_underneath_it(settings):
    ctl = controller(settings)
    ctl.apply(True, [worker_entry("10.0.0.5:8780", name="A")])
    started_with = ctl.lanes(None)
    ctl.apply(True, [])
    assert [n.label for n in started_with] == ["This server", "A"]
    assert [n.label for n in ctl.lanes(None)] == ["This server"]


def test_voice_lists_and_samples_come_from_this_server_when_it_is_on_else_the_first_worker(settings):
    ctl = controller(settings)
    ctl.apply(True, [worker_entry("10.0.0.5:8780")])
    assert ctl.primary.id == LOCAL_ID and ctl.pool is ctl.nodes()[0].pool
    ctl.apply(False, [worker_entry("10.0.0.5:8780")])
    assert ctl.primary.label == "10.0.0.5" and ctl.pool is ctl.nodes()[1].pool


def test_pools_are_made_for_this_server_and_for_each_worker(settings):
    ctl = controller(settings)
    ctl.apply(True, [worker_entry("10.0.0.5:8780")])
    made = ctl.made
    assert [w.url if w else None for w, _ in made] == [None, "http://10.0.0.5:8780"]


def test_removing_a_clone_tells_every_machine_and_survives_one_being_offline(settings, tmp_path):
    from app.remote import WorkerError
    from app.tts import CloneStore
    from conftest import write_wav

    clones = CloneStore(tmp_path / "clones")
    clones.add("Giọng tôi", write_wav(tmp_path / "ref.wav"))
    seen = []

    class Offline(ModelPool):
        def remove_clone(self, name):
            seen.append("offline")
            raise WorkerError("cannot reach")

    class Fine(ModelPool):
        def remove_clone(self, name):
            seen.append("fine")
            return super().remove_clone(name)

    ctl = controller(settings, make_pool=lambda w: (Fine if w is None else Offline)(FakeBackend(), clones))
    ctl.apply(True, [worker_entry("10.0.0.5:8780")])
    assert ctl.remove_clone("Giọng tôi") is True and seen == ["fine", "offline"]
    assert ctl.remove_clone("Giọng tôi") is False


def health(models=None, **extra):
    return {"ok": True, "protocol": 2, "models": models or {"nano": {"ready": True}}, **extra}


def fake_get(responses):
    """httpx.get stand-in: `responses` maps a worker address to a payload, a status code, or an exception."""

    def get(url, headers=None, timeout=None):
        base = url.rsplit("/health", 1)[0]
        answer = responses[base]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, int):
            return httpx.Response(answer, request=httpx.Request("GET", url))
        return httpx.Response(200, json=answer, request=httpx.Request("GET", url))

    return get


def test_status_shows_every_machine_online_or_not_with_the_models_each_has(settings, monkeypatch):
    ctl = controller(settings)
    ctl.apply(True, [worker_entry("10.0.0.5:8780", name="Pixel"), worker_entry("10.0.0.6:8780", name="Laptop"),
                     worker_entry("10.0.0.7:8780", name="Guarded", token="x")])
    monkeypatch.setattr(httpx, "get", fake_get({
        "http://10.0.0.5:8780": health({"nano": {"ready": True}}, capabilities={"synthesize": False, "infer": ["nano"], "clone": False},
                                       name="pixel-8"),
        "http://10.0.0.6:8780": httpx.ConnectError("refused"),
        "http://10.0.0.7:8780": 401}))
    status = {s["name"]: s for s in ctl.status()}
    assert status["This server"]["online"] is True and status["This server"]["kind"] == "local"
    assert status["Pixel"]["online"] is True and status["Pixel"]["models"]["nano"]["ready"] is True
    assert status["Pixel"]["capabilities"]["clone"] is False and status["Pixel"]["latency_ms"] >= 0
    assert status["Laptop"]["online"] is False and "cannot reach" in status["Laptop"]["error"]
    assert status["Guarded"]["online"] is False and "token" in status["Guarded"]["error"]


def test_status_answers_are_reused_for_a_few_seconds(settings, monkeypatch):
    ctl = controller(settings)
    ctl.apply(True, [worker_entry("10.0.0.5:8780")])
    calls = []
    inner = fake_get({"http://10.0.0.5:8780": health()})
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: calls.append(a) or inner(*a, **kw))
    ctl.status(); ctl.status()
    assert len(calls) == 1
    ctl._probes.clear()
    ctl.status()
    assert len(calls) == 2


def test_connection_test_reports_ready_models_latency_and_every_failure_kind(settings, tmp_path, monkeypatch):
    ctl = controller(settings)
    worker, wpool, _ = make_worker(tmp_path)
    wpool.get("nano")
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: worker.get(url.replace("http://worker", ""), headers=headers))
    ok = ctl.test("http://worker", TOKEN)
    assert ok["ok"] is True and ok["models"] == {"nano": True, "turbo": False, "english": False} and ok["latency_ms"] >= 0
    assert ctl.test("http://worker", "wrong") == {"ok": False, "error": "the worker rejected the token"}

    def refuse(url, headers=None, timeout=None):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refuse)
    assert "cannot reach" in ctl.test("http://worker", TOKEN)["error"]


def test_connection_test_reuses_the_saved_token_for_the_saved_worker(settings, monkeypatch):
    ctl = controller(settings)
    wid = ctl.apply(True, [worker_entry("http://worker:1", token="saved-token")])["workers"][0]["id"]
    seen = {}

    def spy(url, headers=None, timeout=None):
        seen.update(headers or {})
        return httpx.Response(200, json={"ok": True, "protocol": 1, "models": {}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", spy)
    ctl.test("http://worker:1", None, wid)
    assert seen["Authorization"] == "Bearer saved-token"
    seen.clear()
    ctl.test("http://worker:1", None)
    assert seen["Authorization"] == "Bearer saved-token"
    seen.clear()
    ctl.test("http://elsewhere:1", None, wid)
    assert "Authorization" not in seen


# --- through the HTTP API ----------------------------------------------------------------

def api(settings, ctl=None):
    ctl = ctl or controller(settings)
    from app.jobs import JobStore

    return TestClient(create_app(settings, JobStore(settings.db), ctl.pool, engine=ctl)), ctl


def test_api_shows_saves_and_validates_the_machine_list(settings):
    c, _ = api(settings)
    first = c.get("/api/engine").json()
    assert first["local"]["enabled"] is True and first["workers"] == [] and set(first["local"]["models"]) == {"nano", "turbo", "english"}
    saved = c.put("/api/engine", json={"local_enabled": True, "workers": [
        {"name": "Pixel", "url": "10.0.0.5:8780", "token": "abc-secret"}]})
    assert saved.status_code == 200 and saved.json()["workers"][0]["url"] == "http://10.0.0.5:8780" and "abc-secret" not in saved.text
    assert c.put("/api/engine", json={"workers": [{"url": "nonsense://"}]}).status_code == 422
    assert c.put("/api/engine", json={"local_enabled": False, "workers": []}).status_code == 422


def test_api_changes_the_machines_even_while_a_book_is_rendering(settings, make_epub):
    from app.jobs import JobStore
    from app.model import State

    store = JobStore(settings.db)
    c, _ = api(settings)
    job = store.create(make_epub(), Voice("nano", None, 0.8, True))
    store.update(job.id, state=State.RUNNING)
    assert c.put("/api/engine", json={"workers": [{"url": "http://x:1"}]}).status_code == 200


def test_api_workers_endpoint_reports_live_status(settings, monkeypatch):
    c, ctl = api(settings)
    c.put("/api/engine", json={"workers": [{"name": "Pixel", "url": "http://10.0.0.5:8780"}]})
    monkeypatch.setattr(httpx, "get", fake_get({"http://10.0.0.5:8780": httpx.ConnectError("refused")}))
    rows = c.get("/api/workers").json()
    assert [(r["name"], r["online"]) for r in rows] == [("This server", True), ("Pixel", False)]


def test_api_connection_test_validates_the_address(settings):
    c, _ = api(settings)
    assert c.post("/api/engine/test", json={"worker_url": "ftp://x"}).status_code == 422
    r = c.post("/api/engine/test", json={"worker_url": "http://127.0.0.1:9"})
    assert r.status_code == 200 and r.json()["ok"] is False and "cannot reach" in r.json()["error"]


def test_voices_come_from_the_first_machine_that_is_on(settings, tmp_path):
    worker, wpool, _ = make_worker(tmp_path)
    wpool.get("nano")

    def make(w):
        if w is None:
            return ModelPool(FakeBackend(), None, settings.voice_cache)
        return ModelPool(RemoteBackend(w.url, w.token, client=worker, sleep=lambda _s: None), None, tmp_path / "remote.json")

    c, ctl = api(settings, controller(settings, make_pool=make))
    ctl.pool.get("nano")
    assert c.get("/api/voices", params={"model": "nano"}).json()["ready"] is True
    c.put("/api/engine", json={"local_enabled": False, "workers": [{"url": "http://worker", "token": TOKEN}]})
    assert ctl.primary.kind == "remote"
    ctl.pool.get("nano")
    r = c.get("/api/voices", params={"model": "nano"}).json()
    assert [v["id"] for v in r["voices"]] == ["Mỹ Duyên", "Anh Khôi"] and r["ready"] is True


def test_voice_list_does_not_ask_the_backend_again_once_loaded(tmp_path):
    class Dying(FakeBackend):
        def load(self, model):
            handle = super().load(model)
            first = handle.list_preset_voices

            def once():
                if getattr(self, "asked", False):
                    raise RuntimeError("the worker is gone")
                self.asked = True
                return first()

            handle.list_preset_voices = once
            return handle

    pool = ModelPool(Dying(), None, tmp_path / "v.json")
    pool.get("nano")
    assert [v["id"] for v in pool.voices("nano").voices] == ["Mỹ Duyên", "Anh Khôi"]
    assert [v["id"] for v in pool.voices("nano").voices] == ["Mỹ Duyên", "Anh Khôi"]


def test_a_dead_worker_gives_a_502_with_the_reason_not_a_crash(settings, tmp_path):
    from test_worker import Flaky

    worker, wpool, _ = make_worker(tmp_path)
    wpool.get("nano")
    flaky = Flaky(worker, 0)
    backend = RemoteBackend("http://worker", TOKEN, client=flaky, sleep=lambda _s: None, retries=0)
    ctl = controller(settings, make_pool=lambda w: ModelPool(backend, None, settings.voice_cache))
    c, _ = api(settings, ctl)
    ctl.pool.get("nano")
    flaky.failures = 10 ** 6
    assert c.get("/api/voices", params={"model": "nano"}).json()["voices"]
    r = c.get("/api/voice-sample", params={"model": "nano", "voice": "Mỹ Duyên"})
    assert r.status_code == 502 and "cannot reach the voice worker" in r.json()["detail"]
