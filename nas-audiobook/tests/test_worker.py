import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.jobs import Runner
from app.model import State, Voice
from app.remote import RemoteBackend, WorkerError
from app.tts import CloneStore, ModelLoading, ModelPool
from app.worker import PROTOCOL, create_worker_app
from conftest import FakeBackend, write_wav

TOKEN = "s3cret"


def make_worker(tmp_path, token=TOKEN, backend=None):
    backend = backend or FakeBackend()
    pool = ModelPool(backend, CloneStore(tmp_path / "worker-clones"), tmp_path / "worker-voices.json")
    return TestClient(create_worker_app(pool, "nano", token), base_url="http://worker"), pool, backend


def auth(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def test_worker_requires_the_token_and_reports_health(tmp_path):
    c, pool, _ = make_worker(tmp_path)
    assert c.get("/health").status_code == 401
    assert c.get("/health", headers=auth("wrong")).status_code == 401
    health = c.get("/health", headers=auth()).json()
    assert health["protocol"] == PROTOCOL and health["models"]["nano"]["ready"] is False
    pool.get("nano")
    assert c.get("/health", headers=auth()).json()["models"]["nano"]["ready"] is True


def test_worker_without_a_token_is_open(tmp_path):
    c, _, _ = make_worker(tmp_path, token=None)
    assert c.get("/health").status_code == 200


def test_worker_voices_wait_for_the_model_then_list_presets(tmp_path):
    c, pool, _ = make_worker(tmp_path)
    r = c.get("/voices", params={"model": "nano"}, headers=auth())
    assert r.status_code in (200, 503)
    pool.get("nano")
    body = c.get("/voices", params={"model": "nano"}, headers=auth()).json()
    assert body["ready"] is True and [v["id"] for v in body["voices"]] == ["Mỹ Duyên", "Anh Khôi"]
    assert c.get("/voices", params={"model": "huge"}, headers=auth()).status_code == 400


def test_synthesize_returns_16_bit_pcm_at_the_models_rate(tmp_path):
    c, pool, backend = make_worker(tmp_path)
    pool.get("nano"); pool.get("turbo")
    r = c.post("/synthesize", json={"model": "nano", "text": "Xin chào", "voice": "Mỹ Duyên", "temperature": 0.7}, headers=auth())
    assert r.status_code == 200 and r.headers["x-sample-rate"] == "24000"
    assert len(r.content) == 2 * int(24000 * 0.01 * len("Xin chào"))
    assert c.post("/synthesize", json={"model": "turbo", "text": "x"}, headers=auth()).headers["x-sample-rate"] == "48000"
    assert backend.calls[0] == ("nano", "Xin chào", "Mỹ Duyên", 0.7)


def test_synthesize_validates_model_and_length(tmp_path):
    c, pool, _ = make_worker(tmp_path)
    pool.get("nano")
    assert c.post("/synthesize", json={"model": "huge", "text": "x"}, headers=auth()).status_code == 400
    assert c.post("/synthesize", json={"model": "nano", "text": "x" * 7000}, headers=auth()).status_code == 400


def test_synthesize_answers_503_while_the_model_loads(tmp_path):
    c, pool, _ = make_worker(tmp_path)
    r = c.post("/synthesize", json={"model": "nano", "text": "x"}, headers=auth())
    assert r.status_code in (200, 503)
    if r.status_code == 503:
        assert r.json()["detail"]["loading"] is True


def test_worker_adds_and_removes_cloned_voices(tmp_path):
    c, pool, backend = make_worker(tmp_path)
    pool.get("nano")
    wav = write_wav(tmp_path / "ref.wav")
    with open(wav, "rb") as f:
        r = c.post("/clones", data={"name": "Giọng tôi"}, files={"file": ("ref.wav", f)}, headers=auth())
    assert r.status_code == 201 and backend.cloned == ["Giọng tôi"]
    names = [v["id"] for v in c.get("/voices", params={"model": "nano"}, headers=auth()).json()["voices"] if v["cloned"]]
    assert names == ["Giọng tôi"]
    assert c.delete("/clones/Giọng tôi", headers=auth()).status_code == 204
    assert c.delete("/clones/Giọng tôi", headers=auth()).status_code == 404 and backend.cloned == []


# --- the NAS side talking to a worker -------------------------------------------------

def remote_pool(tmp_path, worker_client, clones=None, cache=None, **kw):
    backend = RemoteBackend("http://worker", TOKEN, client=worker_client, sleep=lambda _s: None, **kw)
    return ModelPool(backend, clones, cache or tmp_path / "nas-voices.json"), backend


def test_remote_backend_lists_voices_and_returns_audio_matching_the_worker(tmp_path):
    from conftest import FakeHandle

    c, _, fake = make_worker(tmp_path)
    pool, _ = remote_pool(tmp_path, c)
    pool.get("nano")
    assert [v["id"] for v in pool.voices("nano").voices] == ["Mỹ Duyên", "Anh Khôi"]
    audio = pool.synthesize("nano", "Xin chào các bạn", "Mỹ Duyên", 0.8)
    expected = FakeHandle(FakeBackend(), "nano").infer("Xin chào các bạn")
    assert audio.dtype == np.float32 and len(audio) == len(expected) and np.abs(audio - expected).max() < 1e-3
    assert fake.calls[-1][2] == "Mỹ Duyên"


def test_wrong_token_and_protocol_mismatch_give_clear_errors(tmp_path, monkeypatch):
    c, _, _ = make_worker(tmp_path)
    bad = RemoteBackend("http://worker", "nope", client=c, sleep=lambda _s: None)
    with pytest.raises(WorkerError, match="401"):
        bad.load("nano")
    import app.remote as remote

    monkeypatch.setattr(remote, "SUPPORTED_PROTOCOLS", (99,))
    with pytest.raises(WorkerError, match="protocol"):
        RemoteBackend("http://worker", TOKEN, client=c, sleep=lambda _s: None).load("nano")


class Flaky:
    """Wraps a client; the first `failures` calls fail like a dropped Wi-Fi connection."""

    def __init__(self, client, failures):
        self.client, self.failures, self.calls = client, failures, 0

    def request(self, *a, **kw):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("phone went to sleep")
        return self.client.request(*a, **kw)


def test_a_brief_network_drop_is_retried_and_a_long_one_fails_clearly(tmp_path):
    c, pool, _ = make_worker(tmp_path)
    pool.get("nano")
    pauses = []
    ok = RemoteBackend("http://worker", TOKEN, client=Flaky(c, 2), sleep=pauses.append, backoff=1.0)
    handle = ok.load("nano")
    assert len(handle.infer("Xin chào")) > 0 and pauses[:2] == [1.0, 2.0]
    dead = RemoteBackend("http://worker", TOKEN, client=Flaky(c, 99), sleep=lambda _s: None, retries=2)
    with pytest.raises(WorkerError, match="cannot reach the voice worker"):
        dead.load("nano")


def test_nas_uploads_its_cloned_voices_to_the_worker_when_it_connects(tmp_path):
    c, _, fake = make_worker(tmp_path)
    clones = CloneStore(tmp_path / "nas-clones")
    clones.add("Giọng tôi", write_wav(tmp_path / "ref.wav"))
    pool, _ = remote_pool(tmp_path, c, clones)
    pool.get("nano")
    assert fake.cloned == ["Giọng tôi"]
    assert [v["id"] for v in pool.voices("nano").voices if v["cloned"]] == ["Giọng tôi"]


def test_voices_still_show_from_the_saved_list_when_the_worker_is_off(tmp_path):
    c, _, _ = make_worker(tmp_path)
    pool, _ = remote_pool(tmp_path, c)
    pool.get("nano")
    offline, _ = remote_pool(tmp_path, Flaky(c, 10 ** 6), retries=0)
    shown = offline.voices("nano")
    assert [v["id"] for v in shown.voices] == ["Mỹ Duyên", "Anh Khôi"] and shown.ready is False


def test_a_whole_book_converts_through_the_worker(settings, store, make_epub, tmp_path):
    c, _, fake = make_worker(tmp_path)
    pool, _ = remote_pool(tmp_path, c)
    job = store.create(make_epub(), Voice("nano", "Mỹ Duyên", 0.8, True))
    Runner(settings, store, pool.factory()).run_one(store.claim_next())
    done = store.get(job.id)
    assert done.state is State.DONE, done.error
    assert len(list(__import__("pathlib").Path(done.output).glob("*.m4a"))) == 2
    assert any("Anh Nam" in text for _, text, *_ in fake.calls) and all(v == "Mỹ Duyên" for _, _, v, _ in fake.calls)
