"""The server and a worker start without any model: nothing is loaded or downloaded until something needs it."""
import pytest
from fastapi.testclient import TestClient

from app import catalog
from app.api import create_app
from app.engine_config import EngineConfig, EngineConfigStore, EngineController
from app.jobs import JobStore
from app.tts import ModelPool
from conftest import FakeBackend
from test_worker import auth, make_worker


def test_the_preload_setting_means_nothing_by_default():
    for raw in (None, "", "  ", "none", "None", " none , "):
        assert catalog.parse_model_list(raw) == []


def test_it_lists_the_models_to_load_in_order_without_repeats():
    assert catalog.parse_model_list("nano, english,NANO") == ["nano", "english"]
    assert catalog.parse_model_list("turbo,none") == ["turbo"]


def test_an_unknown_model_name_is_an_error_naming_the_choices():
    with pytest.raises(ValueError, match="klingon.*nano"):
        catalog.parse_model_list("nano,klingon")


def test_starting_the_server_and_opening_every_page_loads_no_model(settings):
    backend = FakeBackend()
    ctl = EngineController(EngineConfigStore(settings.engine_config), EngineConfig(),
                           lambda w: ModelPool(backend, None, settings.voice_cache))
    c = TestClient(create_app(settings, JobStore(settings.db), ctl.pool, engine=ctl))
    for path in ("/", "/healthz", "/api/defaults", "/api/engine", "/api/workers", "/api/models", "/api/processors",
                 "/api/jobs", "/api/library", "/api/calibre"):
        assert c.get(path).status_code == 200, path
    assert backend.loads == []
    states = {m: s["state"] for m, s in c.get("/api/workers").json()[0]["models"].items()}
    assert set(states.values()) <= {"absent", "downloaded"}


def test_a_worker_starts_with_nothing_loaded_and_answers_without_loading(tmp_path):
    c, pool, backend = make_worker(tmp_path)
    assert c.get("/health", headers=auth()).status_code == 200 and c.get("/models", headers=auth()).status_code == 200
    assert backend.loads == []
