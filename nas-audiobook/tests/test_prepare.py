"""Making a machine ready for the voice a conversion chose: the model downloaded and loaded, the voice present."""
import time

import pytest
from fastapi.testclient import TestClient

from app.engine_config import EngineConfig, EngineConfigStore, EngineController
from app.fleet import Node, WorkerConfig
from app.jobs import Runner
from app.library import Library, md5_file
from app.model import State, Voice
from app.remote import RemoteBackend, WorkerError
from app.tts import CloneStore, ModelPool, VoiceMissing
from app.worker import create_worker_app
from conftest import FILLER, FakeBackend, write_wav
from test_worker import TOKEN, make_worker


@pytest.fixture
def clones(tmp_path):
    store = CloneStore(tmp_path / "clones")
    store.add("Giọng tôi", write_wav(tmp_path / "ref.wav"))
    return store


def test_preparing_loads_the_model_and_accepts_voices_it_has():
    backend = FakeBackend()
    pool = ModelPool(backend)
    messages = []
    pool.prepare(Voice("nano", "Mỹ Duyên", (), True, "Anh Khôi"), messages.append)
    assert backend.loads == ["nano"] and pool.ready("nano")
    assert any("loading the Vietnamese · Nano model" in m for m in messages) and "checking voices" in messages


def test_the_default_voice_needs_no_voice_check():
    backend = FakeBackend()
    ModelPool(backend).prepare(Voice("nano", None, (), True))
    assert backend.loads == ["nano"]


def test_a_preset_voice_the_machine_does_not_have_is_reported_by_name():
    with pytest.raises(VoiceMissing, match="'Giọng lạ'"):
        ModelPool(FakeBackend()).prepare(Voice("nano", "Giọng lạ", (), True))


def test_a_cloned_voice_the_machine_never_saw_is_sent_to_it(clones):
    backend = FakeBackend()
    pool = ModelPool(backend, clones)
    pool.get("nano")
    backend.cloned.clear()  # the machine lost it, or never had it
    messages = []
    pool.prepare(Voice("nano", "Giọng tôi", (), True), messages.append)
    assert backend.cloned == ["Giọng tôi"] and any("sending the cloned voice" in m for m in messages)


def test_a_machine_that_cannot_clone_says_so_instead_of_getting_the_voice(clones):
    backend = FakeBackend()
    pool = ModelPool(backend, clones)
    pool.get("nano").can_clone = False
    backend.cloned.clear()
    with pytest.raises(VoiceMissing, match="Giọng tôi"):
        pool.prepare(Voice("nano", "Giọng tôi", (), True))


def remote_node(tmp_path, worker_client, clones, name="Pixel"):
    backend = RemoteBackend("http://worker", TOKEN, client=worker_client, sleep=lambda _s: None)
    pool = ModelPool(backend, clones, tmp_path / "remote-voices.json")
    return Node("w1", name, pool, WorkerConfig("w1", name, "http://worker", TOKEN), True)


def test_a_worker_that_has_not_loaded_the_model_is_told_to_and_the_voice_is_uploaded(tmp_path, clones):
    worker, wpool, wbackend = make_worker(tmp_path)
    assert not wpool.ready("nano")
    node = remote_node(tmp_path, worker, clones)
    engine = node.engine(Voice("nano", "Giọng tôi", (), True))
    assert wpool.ready("nano") and wbackend.loads == ["nano"]
    assert wbackend.cloned == ["Giọng tôi"] and engine.sample_rate == 24000
    assert len(engine.synthesize("Xin chào các bạn")) > 0


def test_a_worker_that_already_has_everything_is_not_sent_the_voice_again(tmp_path, clones):
    worker, wpool, wbackend = make_worker(tmp_path)
    wpool.get("nano")
    with open(write_wav(tmp_path / "again.wav"), "rb") as f:
        assert worker.post("/clones", data={"name": "Giọng tôi"}, files={"file": ("a.wav", f)},
                           headers={"Authorization": f"Bearer {TOKEN}"}).status_code == 201
    assert wbackend.cloned == ["Giọng tôi"]
    remote_node(tmp_path, worker, clones).engine(Voice("nano", "Giọng tôi", (), True))
    assert wbackend.cloned == ["Giọng tôi"]


def test_a_phone_that_only_runs_nano_refuses_turbo_with_a_clear_reason(tmp_path, clones):
    phone = TestClient(create_worker_app(ModelPool(FakeBackend(), None, tmp_path / "p.json"), "nano", TOKEN, "phonemes"),
                       base_url="http://worker")
    node = remote_node(tmp_path, phone, clones)
    with pytest.raises(WorkerError, match="cannot run the turbo model"):
        node.engine(Voice("turbo", None, (), True))


def test_a_phone_cannot_be_given_a_cloned_voice(tmp_path, clones):
    from test_phonemes import StubFrontend

    phone = TestClient(create_worker_app(ModelPool(FakeBackend(), None, tmp_path / "p.json"), "nano", TOKEN, "phonemes"),
                       base_url="http://worker")
    backend = RemoteBackend("http://worker", TOKEN, client=phone, sleep=lambda _s: None, frontend=StubFrontend())
    node = Node("w1", "Phone", ModelPool(backend, clones, tmp_path / "pv.json"), WorkerConfig("w1", "Phone", "http://worker"), True)
    with pytest.raises(VoiceMissing, match="Giọng tôi"):
        node.engine(Voice("nano", "Giọng tôi", (), True))


class Slow(FakeBackend):
    """Each synthesis takes a moment, so two machines really do share a book."""

    def load(self, model):
        handle = super().load(model)
        infer = handle.infer

        def slow(*a, **kw):
            time.sleep(0.01)
            return infer(*a, **kw)

        handle.infer = slow
        return handle


def test_a_book_is_shared_between_this_server_and_a_worker_end_to_end(settings, store, make_epub, tmp_path, clones):
    worker, wpool, wbackend = make_worker(tmp_path, backend=Slow())
    local = Slow()

    def make(w):
        if w is None:
            return ModelPool(local, clones, tmp_path / "local-voices.json")
        return ModelPool(RemoteBackend(w.url, w.token, client=worker, sleep=lambda _s: None), clones, tmp_path / "remote.json")

    ctl = EngineController(EngineConfigStore(settings.engine_config), EngineConfig(True, (WorkerConfig("w1", "Pixel", "http://worker", TOKEN),)), make)
    book = make_epub("shared.epub", [(f"Chương {k}", [f"Mốc số {k}.", FILLER, FILLER]) for k in range(1, 7)])
    job = store.create(book, Voice("nano", "Giọng tôi", (), True))
    Runner(settings, store, ctl).run_one(store.claim_next())
    done = store.get(job.id)
    assert done.state is State.DONE, done.error
    assert sorted(e.index for e in Library(settings.library).entries(md5_file(book))) == list(range(6))
    assert local.calls and wbackend.calls, "both machines should have rendered chapters"
    assert wbackend.cloned == ["Giọng tôi"] and local.cloned == ["Giọng tôi"]
    assert {w["label"] for w in done.public()["workers"]} == {"This server", "Pixel"}
