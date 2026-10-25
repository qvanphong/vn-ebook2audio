"""Workers own their models: a worker serves the models it chose, fetches what it lacks itself, and says how it is doing."""
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app import catalog
from app.api import create_app
from app.engine_config import ConfigError, EngineConfig, EngineConfigStore, EngineController, WorkerOwnsModels
from app.fleet import Node, WorkerConfig, probe
from app.jobs import JobStore, Runner
from app.library import md5_file
from app.model import State, Voice
from app.remote import RemoteBackend, WorkerError
from app.tts import ModelPool
from app.worker import PROTOCOL, create_worker_app
from conftest import FILLER, FakeBackend
from test_engine import fake_get
from test_worker import TOKEN, auth, make_worker

pytestmark = pytest.mark.usefixtures("no_real_models")


@pytest.fixture
def no_real_models(monkeypatch):
    monkeypatch.setattr(catalog, "package_installed", lambda model: True)


class Downloading(FakeBackend):
    """A backend that has to fetch a model first: `release` ends the download, `arrived` is what has come so far."""

    def __init__(self, have=False):
        super().__init__()
        self.have, self.arrived, self.release = have, 0, threading.Event()

    def downloaded(self, model):
        return self.have

    def bytes_on_disk(self, model):
        return self.arrived

    def supported(self, model):
        return True

    def load(self, model):
        if not self.have:
            self.arrived = 120_000_000
            assert self.release.wait(10), "the test never released the download"
            self.have = True
        return super().load(model)


def worker_with(backend, tmp_path, accepts="both", offered=None):
    pool = ModelPool(backend, None, tmp_path / "w.json")
    return TestClient(create_worker_app(pool, "nano", TOKEN, accepts, offered=offered), base_url="http://worker"), pool


def wait(predicate, seconds=3):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


# --- what a machine has --------------------------------------------------------------------

def fake_cache(monkeypatch, present):
    monkeypatch.setattr("huggingface_hub.try_to_load_from_cache",
                        lambda repo, name, **kw: f"/cache/{repo}/{name}" if (repo, name) in present else None)


def test_nano_counts_as_downloaded_only_when_every_graph_is_there(monkeypatch):
    every = {(catalog.NANO_REPO, f) for f in catalog.NANO_FILES}
    fake_cache(monkeypatch, every)
    assert catalog.downloaded("nano") is True
    fake_cache(monkeypatch, every - {(catalog.NANO_REPO, "vector_estimator.onnx")})
    assert catalog.downloaded("nano") is False


def test_turbo_needs_the_files_of_the_precision_in_use_and_the_codec(monkeypatch):
    have = {(catalog.TURBO_REPO, f"onnx_int8/{f}") for f in catalog.TURBO_FILES} | {(catalog.MOSS_REPO, f) for f in catalog.MOSS_FILES}
    fake_cache(monkeypatch, have)
    assert catalog.downloaded("turbo", "int8") is True and catalog.downloaded("turbo", "fp32") is False
    fake_cache(monkeypatch, {x for x in have if x[0] != catalog.MOSS_REPO})
    assert catalog.downloaded("turbo", "int8") is False


def test_english_counts_as_downloaded_when_supertonics_graphs_are_in_its_folder(tmp_path, monkeypatch):
    pytest.importorskip("supertonic")
    monkeypatch.setenv("SUPERTONIC_CACHE_DIR", str(tmp_path / "st"))
    assert catalog.downloaded("english") is False and catalog.bytes_on_disk("english") == 0
    onnx = tmp_path / "st" / "onnx"
    onnx.mkdir(parents=True)
    for name in ("duration_predictor", "text_encoder", "vector_estimator", "vocoder"):
        (onnx / f"{name}.onnx").write_bytes(b"x" * 1000)
    assert catalog.downloaded("english") is True and catalog.bytes_on_disk("english") == 4000
    partial = tmp_path / ".st.tmp"
    partial.mkdir()
    (partial / "half.onnx").write_bytes(b"y" * 500)
    assert catalog.bytes_on_disk("english") == 4500  # a download in progress counts


def test_the_cache_folders_follow_hf_home_so_one_mounted_folder_holds_everything(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPERTONIC_CACHE_DIR", raising=False)
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    catalog.configure_cache_env()
    import os
    assert os.environ["SUPERTONIC_CACHE_DIR"] == str(tmp_path / "hf" / "supertonic3")
    monkeypatch.setenv("SUPERTONIC_CACHE_DIR", "/elsewhere")
    catalog.configure_cache_env()
    assert os.environ["SUPERTONIC_CACHE_DIR"] == "/elsewhere"


def test_an_unknown_model_is_an_error():
    with pytest.raises(ValueError):
        catalog.downloaded("klingon")


def test_the_pool_tells_absent_from_downloaded_downloading_loading_ready_and_error():
    backend = Downloading()
    pool = ModelPool(backend)
    assert pool.status()["english"]["state"] == "absent" and pool.status()["english"]["size_mb"] == 390
    backend.have = True
    assert pool.status()["english"]["state"] == "downloaded"
    backend.have = False
    pool.start_loading("english")
    assert wait(lambda: pool.status()["english"]["state"] == "downloading")
    assert pool.status()["english"]["bytes"] == 120_000_000
    backend.release.set()
    assert wait(lambda: pool.status()["english"]["state"] == "ready")

    class Broken(Downloading):
        def load(self, model):
            raise RuntimeError("disk full")

    broken = ModelPool(Broken(have=True))
    broken.start_loading("nano")
    assert wait(lambda: broken.status()["nano"]["state"] == "error") and "disk full" in broken.status()["nano"]["error"]


def test_progress_counts_only_what_arrived_since_the_download_began():
    backend = Downloading()
    backend.arrived = 50_000_000  # left over from an earlier, shared download
    pool = ModelPool(backend)
    pool.start_loading("nano")
    assert wait(lambda: pool.status()["nano"]["state"] == "downloading")
    assert pool.status()["nano"]["bytes"] == 70_000_000
    backend.release.set()


def test_preparing_says_whether_it_will_download_or_only_load():
    messages = []
    backend = Downloading()
    backend.release.set()
    ModelPool(backend).prepare(Voice("english", None, (), True), messages.append)
    assert messages[0] == "downloading the English · Supertonic model (about 390 MB), then loading it"
    messages.clear()
    ModelPool(Downloading(have=True)).prepare(Voice("nano", None, (), True), messages.append)
    assert messages[0] == "loading the Vietnamese · Nano model"


# --- the worker ----------------------------------------------------------------------------

def test_a_worker_lists_every_model_and_what_it_can_do_with_each(tmp_path):
    c, pool = worker_with(Downloading(have=True), tmp_path)
    assert c.get("/health", headers=auth()).json()["protocol"] == PROTOCOL == 4
    models = c.get("/models", headers=auth()).json()["models"]
    assert set(models) == {"nano", "turbo", "english"}
    assert models["nano"]["state"] == "downloaded" and models["nano"]["supported"] is True and models["nano"]["size_mb"] == 280
    assert c.get("/models").status_code == 401


def test_a_phone_style_worker_says_which_models_it_cannot_run(tmp_path):
    c, _ = worker_with(Downloading(), tmp_path, accepts="phonemes")
    models = c.get("/models", headers=auth()).json()["models"]
    assert {m: s["supported"] for m, s in models.items()} == {"nano": True, "turbo": False, "english": False}


def test_a_worker_without_the_package_says_it_does_not_support_the_model(tmp_path, monkeypatch):
    class NoEnglish(Downloading):
        def supported(self, model):
            return model != "english"

    c, _ = worker_with(NoEnglish(), tmp_path)
    assert c.get("/models", headers=auth()).json()["models"]["english"]["supported"] is False


def test_a_worker_fetches_a_model_itself_when_asked_for_it_and_asking_twice_changes_nothing(tmp_path):
    backend = Downloading()
    c, pool = worker_with(backend, tmp_path)
    assert c.get("/voices", params={"model": "english"}, headers=auth()).status_code == 503
    assert wait(lambda: c.get("/models", headers=auth()).json()["models"]["english"]["state"] == "downloading")
    assert c.get("/voices", params={"model": "english"}, headers=auth()).status_code == 503
    backend.release.set()
    assert wait(lambda: c.get("/models", headers=auth()).json()["models"]["english"]["state"] == "ready")
    assert backend.loads == ["english"]


def test_the_server_has_no_way_to_tell_a_worker_to_download(tmp_path):
    c, _ = worker_with(Downloading(), tmp_path)
    assert c.post("/models/english/ensure", headers=auth()).status_code in (404, 405)


# --- the worker decides which models it serves ----------------------------------------------

def test_a_worker_serves_only_the_models_it_was_given_and_says_so(tmp_path):
    backend = Downloading(have=True)
    c, pool = worker_with(backend, tmp_path, offered=["nano"])
    models = c.get("/models", headers=auth()).json()["models"]
    assert {m: (s["offered"], s["supported"]) for m, s in models.items()} == {
        "nano": (True, True), "turbo": (False, False), "english": (False, False)}
    assert c.get("/voices", params={"model": "english"}, headers=auth()).status_code == 501
    assert c.post("/synthesize", json={"model": "turbo", "text": "Xin chào"}, headers=auth()).status_code == 501
    assert c.post("/infer", json={"model": "english", "phonemes": "a"}, headers=auth()).status_code == 501
    assert backend.loads == []  # a model it does not serve is never fetched
    assert c.get("/voices", params={"model": "nano"}, headers=auth()).status_code in (200, 503)


def test_the_server_will_not_use_a_worker_for_a_model_it_does_not_serve(tmp_path):
    c, _ = worker_with(Downloading(have=True), tmp_path, offered=["nano"])
    with pytest.raises(WorkerError, match="does not serve the English · Supertonic model"):
        remote(c).await_model("english")


def test_a_worker_that_serves_nothing_cloneable_refuses_clones(tmp_path):
    c, _ = worker_with(Downloading(have=True), tmp_path, offered=["english"])
    r = c.post("/clones", data={"name": "me"}, files={"file": ("a.wav", b"x")}, headers=auth())
    assert r.status_code == 501


def test_a_worker_models_setting_names_models_or_all():
    assert catalog.parse_offered(None) == catalog.parse_offered("all") == ["nano", "turbo", "english"]
    assert catalog.parse_offered("Nano, english") == ["nano", "english"]
    with pytest.raises(ValueError, match="unknown model"):
        catalog.parse_offered("nano,klingon")
    with pytest.raises(ValueError, match="names no model"):
        catalog.parse_offered("none")


def test_a_worker_loads_nothing_until_it_is_asked(tmp_path):
    backend = Downloading(have=True)
    c, pool = worker_with(backend, tmp_path)
    c.get("/health", headers=auth()); c.get("/models", headers=auth())
    assert backend.loads == [] and not any(pool.ready(m) for m in ("nano", "turbo", "english"))


# --- the server asking ---------------------------------------------------------------------

def remote(c, **kw):
    return RemoteBackend("http://worker", TOKEN, client=c, sleep=kw.pop("sleep", lambda _s: None), **kw)


def test_the_server_waits_for_the_download_and_reports_each_stage(tmp_path):
    backend = Downloading()
    c, pool = worker_with(backend, tmp_path)
    polls, messages = [], []

    def sleep(_s):
        polls.append(1)
        if len(polls) == 4:
            backend.release.set()
        time.sleep(0.02)

    remote(c, sleep=sleep).await_model("english", messages.append)
    assert pool.ready("english")
    assert messages[0].startswith("waiting for the worker to get the English · Supertonic model")
    assert any("the worker is downloading the English · Supertonic model (120 of about 390 MB)" == m for m in messages)
    assert backend.loads == ["english"]


def test_a_model_the_worker_already_has_goes_straight_through(tmp_path):
    backend = Downloading(have=True)
    c, pool = worker_with(backend, tmp_path)
    pool.get("nano")
    messages = []
    remote(c).await_model("nano", messages.append)
    assert messages == [] and backend.loads == ["nano"]


def test_a_worker_that_has_it_on_disk_only_has_to_load_it(tmp_path):
    backend = Downloading(have=True)
    c, pool = worker_with(backend, tmp_path)
    messages = []
    remote(c, poll_seconds=0).await_model("nano", messages.append)
    assert pool.ready("nano") and not any("downloading" in m for m in messages)


def test_a_worker_that_cannot_do_it_or_fails_is_reported_with_the_reason(tmp_path):
    c, _ = worker_with(Downloading(), tmp_path, accepts="phonemes")
    with pytest.raises(WorkerError, match="cannot run the english model"):
        remote(c).await_model("english")

    class Broken(Downloading):
        def load(self, model):
            raise RuntimeError("no space left on device")

    c2, _ = worker_with(Broken(have=True), tmp_path)
    with pytest.raises(WorkerError, match="could not load the Vietnamese · Nano model: .*no space left"):
        remote(c2).await_model("nano")


def test_the_wait_has_a_limit(tmp_path):
    backend = Downloading()
    c, _ = worker_with(backend, tmp_path)
    with pytest.raises(WorkerError, match="did not finish getting"):
        remote(c, load_timeout=0.05, sleep=lambda _s: time.sleep(0.03)).await_model("english")
    backend.release.set()


class OldWorker:
    """A protocol 2 worker: no /models, and a model loads when its voices are asked for."""

    def __init__(self, client):
        self.client = client

    def request(self, method, path, **kw):
        if path.startswith("/models"):
            return httpx.Response(404, json={"detail": "Not Found"})
        return self.client.request(method, path, **kw)


def test_an_older_worker_still_works_through_the_voice_request(tmp_path):
    backend = Downloading(have=True)
    c, pool = worker_with(backend, tmp_path)
    b = remote(OldWorker(c))
    assert b.models() is None
    b.await_model("nano")  # nothing to do: load() polls /voices as before
    handle = ModelPool(b, None, tmp_path / "n.json").get("nano")
    assert pool.ready("nano") and len(handle.infer("Xin chào")) > 0


def test_a_machine_gets_ready_for_the_voice_and_model_in_the_request_end_to_end(tmp_path):
    backend = Downloading()
    c, wpool = worker_with(backend, tmp_path)
    b = remote(c, sleep=lambda _s: (backend.release.set(), time.sleep(0.01)) and None)
    node = Node("w", "Laptop", ModelPool(b, None, tmp_path / "n.json"), WorkerConfig("w", "Laptop", "http://worker"), True)
    engine = node.engine(Voice("english", "Mỹ Duyên", (), True))
    assert wpool.ready("english") and engine.sample_rate == 44100 and len(engine.synthesize("Hello there, reader.")) > 0
    assert backend.calls[-1][:3] == ("english", "Hello there, reader.", "Mỹ Duyên")


# --- the page's side -----------------------------------------------------------------------

def controller(settings, backend=None):
    def make(worker):
        if worker is None:
            return ModelPool(backend or Downloading(have=True), None, settings.voice_cache)
        return ModelPool(RemoteBackend(worker.url, worker.token), None, settings.voice_cache.with_name(f"{worker.id}.json"))

    return EngineController(EngineConfigStore(settings.engine_config), EngineConfig(), make)


def test_the_status_shows_each_workers_models_as_the_worker_reports_them(settings, monkeypatch):
    ctl = controller(settings)
    ctl.apply(True, [{"url": "10.0.0.5:8780", "name": "Pixel"}])
    states = {"nano": {"ready": False, "state": "downloaded", "supported": True, "downloaded": True, "bytes": 0, "size_mb": 280},
              "english": {"ready": False, "state": "absent", "supported": True, "downloaded": False, "bytes": 0, "size_mb": 390}}

    def get(url, headers=None, timeout=None):
        if url.endswith("/models"):
            return httpx.Response(200, json={"models": states}, request=httpx.Request("GET", url))
        return httpx.Response(200, json={"ok": True, "protocol": 3, "models": {}}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get)
    pixel = [s for s in ctl.status() if s["name"] == "Pixel"][0]
    assert pixel["models"]["english"]["state"] == "absent" and pixel["models"]["nano"]["downloaded"] is True
    local = ctl.status()[0]
    assert local["models"]["nano"]["state"] == "downloaded" and local["models"]["english"]["size_mb"] == 390


def test_an_older_worker_still_shows_ready_or_not(settings, monkeypatch):
    ctl = controller(settings)
    ctl.apply(True, [{"url": "10.0.0.5:8780", "name": "Old"}])
    monkeypatch.setattr(httpx, "get", fake_get({"http://10.0.0.5:8780": {"ok": True, "protocol": 2, "models": {"nano": {"ready": True}}}}))
    old = [s for s in ctl.status() if s["name"] == "Old"][0]
    assert old["models"]["nano"] == {"ready": True}


def api(settings, ctl):
    return TestClient(create_app(settings, JobStore(settings.db), ctl.pool, engine=ctl))


def test_the_api_lists_the_catalog_and_fetches_a_model_on_a_machine(settings):
    backend = Downloading()
    ctl = controller(settings, backend)
    c = api(settings, ctl)
    cat = {m["id"]: m for m in c.get("/api/models").json()}
    assert cat["english"] == {"id": "english", "label": "English · Supertonic", "language": "en",
                              "description": "Clear and fast, 10 voices, no cloning", "size_mb": 390,
                              "accelerators": ["cuda", "directml", "coreml"]}
    r = c.post("/api/workers/local/models/english")
    assert r.status_code == 202 and r.json() == {"id": "local", "model": "english"}
    assert wait(lambda: ctl.pool.status()["english"]["state"] == "downloading")
    backend.release.set()
    assert wait(lambda: ctl.pool.ready("english"))
    assert c.post("/api/workers/nobody/models/english").status_code == 404
    assert c.post("/api/workers/local/models/klingon").status_code == 404


def test_a_worker_is_never_told_to_fetch_a_model_it_chooses_its_own(settings, tmp_path):
    wc, wpool = worker_with(Downloading(have=True), tmp_path)
    ctl = EngineController(EngineConfigStore(settings.engine_config), EngineConfig(),
                           lambda w: ModelPool(Downloading(have=True) if w is None else remote(wc), None,
                                               settings.voice_cache.with_name("x.json")))
    ctl.apply(True, [{"url": "http://worker", "token": TOKEN, "name": "Laptop"}])
    wid = ctl.config.workers[0].id
    with pytest.raises(WorkerOwnsModels, match="Laptop chooses and downloads its own models"):
        ctl.fetch_model(wid, "nano")
    assert api(settings, ctl).post(f"/api/workers/{wid}/models/nano").status_code == 409
    assert not wpool.ready("nano")


# --- a conversion that has to wait for a download ------------------------------------------

def test_a_conversion_shows_the_download_then_starts_and_finishes(settings, store, make_epub, tmp_path):
    backend = Downloading()
    wc, wpool = worker_with(backend, tmp_path)
    seen = []

    def sleep(_s):
        job = store.get(job_id)
        seen.extend(w["detail"] for w in job.public()["workers"] if w["detail"])
        if len([d for d in seen if "downloading" in d]) >= 2:
            backend.release.set()
        time.sleep(0.02)

    ctl = EngineController(EngineConfigStore(settings.engine_config),
                           EngineConfig(False, (WorkerConfig("w", "Laptop", "http://worker", TOKEN),)),
                           lambda w: ModelPool(remote(wc, sleep=sleep), None, tmp_path / "z.json"))
    book = make_epub("dl.epub", [("Chapter 1", ["Hello there, dear reader of this little book.", FILLER])], language="en")
    job_id = store.create(book, Voice("english", "Mỹ Duyên", (), True)).id
    Runner(settings, store, ctl).run_one(store.claim_next())
    done = store.get(job_id)
    assert done.state is State.DONE, done.error
    assert any(d.startswith("waiting for the worker to get the English") for d in seen) or any("downloading" in d for d in seen)
    assert any("120 of about 390 MB" in d for d in seen)
    assert backend.loads == ["english"] and wpool.ready("english")
    assert backend.calls and backend.calls[0][0] == "english" and backend.calls[0][2] == "Mỹ Duyên"
