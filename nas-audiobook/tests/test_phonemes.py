import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.frontend import Frontend, FrontendMissing
from app.jobs import Runner
from app.model import State, Voice
from app.remote import RemoteBackend, WorkerError
from app.tts import CloneError, CloneStore, LocalHandle, ModelPool, Unsupported
from app.worker import PROTOCOL, create_worker_app
from conftest import FakeBackend, write_wav
from test_worker import TOKEN, auth


class StubFrontend:
    """Two chunks per text, a pause between them, phonemes spelled PH(...)."""

    def chunks(self, text, max_chars=140):
        half = max(1, len(text) // 2)
        return [text[:half], text[half:]], ["sentence"]

    def phonemes(self, chunk):
        return f"PH({chunk})"

    def join(self, wavs, sample_rate, gaps):
        return np.concatenate([wavs[0], np.zeros(int(sample_rate * 0.5), dtype=np.float32), wavs[1]])


def phone_worker(tmp_path, accepts="phonemes"):
    backend = FakeBackend()
    pool = ModelPool(backend, CloneStore(tmp_path / "worker-clones"), tmp_path / "worker-voices.json")
    pool.get("nano")
    return TestClient(create_worker_app(pool, "nano", TOKEN, accepts), base_url="http://phone"), backend


def nas_pool(tmp_path, client, clones=None, frontend=None):
    backend = RemoteBackend("http://phone", TOKEN, client=client, sleep=lambda _s: None,
                            frontend=frontend if frontend is not None else StubFrontend())
    return ModelPool(backend, clones, tmp_path / "nas-voices.json")


def test_health_reports_what_the_worker_can_do(tmp_path):
    both, _ = phone_worker(tmp_path, "both")
    phone, _ = phone_worker(tmp_path, "phonemes")
    assert both.get("/health", headers=auth()).json()["capabilities"] == {"synthesize": True, "infer": ["nano"], "clone": True,
                                                                          "batch": True}
    h = phone.get("/health", headers=auth()).json()
    assert h["protocol"] == PROTOCOL == 4
    assert h["capabilities"] == {"synthesize": False, "infer": ["nano"], "clone": False, "batch": False}


def test_infer_returns_one_chunk_of_pcm_and_passes_the_options_through(tmp_path):
    c, backend = phone_worker(tmp_path)
    r = c.post("/infer", json={"model": "nano", "phonemes": "xin tʃaː", "voice": "Mỹ Duyên", "steps": 8, "cfg": 2.0,
                               "speed": 1.1, "seed": 5}, headers=auth())
    assert r.status_code == 200 and r.headers["x-sample-rate"] == "24000"
    assert len(r.content) == 2 * int(24000 * 0.01 * len("xin tʃaː"))
    assert backend.phoneme_calls == [("nano", "xin tʃaː", "Mỹ Duyên", 8, 2.0, 1.1, 5)]


def test_infer_validates_input_and_refuses_models_it_cannot_do(tmp_path):
    c, _ = phone_worker(tmp_path)
    assert c.post("/infer", json={"model": "huge", "phonemes": "x"}, headers=auth()).status_code == 400
    assert c.post("/infer", json={"model": "nano", "phonemes": "x" * 7000}, headers=auth()).status_code == 400
    assert c.post("/infer", json={"model": "turbo", "phonemes": "x"}, headers=auth()).status_code in (501, 503)
    assert c.post("/infer", json={"model": "nano", "phonemes": "x"}).status_code == 401


def test_a_phone_like_worker_refuses_text_and_cloning(tmp_path):
    c, _ = phone_worker(tmp_path)
    assert c.post("/synthesize", json={"model": "nano", "text": "x"}, headers=auth()).status_code == 501
    wav = write_wav(tmp_path / "r.wav")
    with open(wav, "rb") as f:
        assert c.post("/clones", data={"name": "x"}, files={"file": ("r.wav", f)}, headers=auth()).status_code == 501


def test_nas_does_the_text_work_and_the_phone_only_sees_phonemes(tmp_path):
    c, backend = phone_worker(tmp_path)
    pool = nas_pool(tmp_path, c)
    audio = pool.synthesize("nano", "abcdefgh", "Mỹ Duyên", 0.8)
    assert [call[1] for call in backend.phoneme_calls] == ["PH(abcd)", "PH(efgh)"]
    assert all(call[2] == "Mỹ Duyên" for call in backend.phoneme_calls)
    pause = int(24000 * 0.5)
    assert len(audio) == int(24000 * 0.01 * len("PH(abcd)")) * 2 + pause and not backend.calls


def test_a_whole_book_converts_through_a_phone_like_worker(settings, store, make_epub, tmp_path):
    c, backend = phone_worker(tmp_path)
    pool = nas_pool(tmp_path, c)
    job = store.create(make_epub(), Voice("nano", "Mỹ Duyên", 0.8, True))
    Runner(settings, store, pool.factory()).run_one(store.claim_next())
    done = store.get(job.id)
    assert done.state is State.DONE, done.error
    assert len(list(__import__("pathlib").Path(done.output).glob("*.m4a"))) == 2
    assert backend.phoneme_calls and not backend.calls


def test_cloning_is_switched_off_when_the_worker_cannot_clone(tmp_path):
    c, backend = phone_worker(tmp_path)
    clones = CloneStore(tmp_path / "nas-clones")
    clones.add("Giọng tôi", write_wav(tmp_path / "ref.wav"))
    pool = nas_pool(tmp_path, c, clones)
    pool.get("nano")
    assert backend.cloned == [] and pool.can_clone("nano") is False
    assert [v["id"] for v in pool.voices("nano").voices if v["cloned"]] == []
    with pytest.raises(CloneError, match="cannot clone"):
        pool.add_clone("Khác", write_wav(tmp_path / "other.wav"), "nano")


def test_missing_text_frontend_is_reported_with_install_instructions(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def refuse(name, *a, **kw):
        if name.startswith("vieneu_utils"):
            raise ImportError(name)
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", refuse)
    with pytest.raises(FrontendMissing, match="pip install --no-deps vieneu"):
        Frontend()


def test_a_worker_that_cannot_run_the_model_is_rejected_when_connecting(tmp_path):
    c, _ = phone_worker(tmp_path)
    backend = RemoteBackend("http://phone", TOKEN, client=c, sleep=lambda _s: None, frontend=StubFrontend())
    with pytest.raises(WorkerError, match="cannot run the turbo model"):
        backend.load("turbo")


def test_the_real_frontend_chunks_phonemises_and_joins_vietnamese(tmp_path):
    pytest.importorskip("vieneu_utils")
    pytest.importorskip("sea_g2p")
    f = Frontend()
    text = "Xin chào các bạn. Hôm nay là ngày 22/10/2026, chúng ta cùng nghe một câu chuyện nhé, về hành trình của một người đi tìm sự thật."
    chunks, gaps = f.chunks(text)
    assert len(chunks) >= 2 and len(gaps) == len(chunks) - 1
    assert "hai mươi hai" in " ".join(chunks)
    ph = [f.phonemes(c) for c in chunks]
    assert all(p.strip() for p in ph)
    wavs = [np.full(2400, 0.1, dtype=np.float32) for _ in chunks]
    joined = f.join(wavs, 24000, gaps)
    assert len(joined) >= sum(len(w) for w in wavs)


class FakeEngine:
    def __init__(self):
        self.calls = []

    def infer(self, phonemes, speaker, style, **kw):
        self.calls.append((phonemes, speaker, style, kw))
        return np.zeros(10, dtype=np.float32)


class FakeNano:
    default_steps, default_cfg = 16, 3.0

    def __init__(self):
        self.engine = FakeEngine()

    def _resolve_voice(self, voice, ref_audio):
        return f"spk:{voice}", f"style:{voice}"


def test_local_handle_runs_phonemes_through_the_nano_engine_with_its_defaults():
    nano = FakeNano()
    LocalHandle("nano", nano).infer_phonemes("xin tʃaː", voice="Adam", speed=1.2, seed=3)
    phonemes, speaker, style, kw = nano.engine.calls[0]
    assert (phonemes, speaker, style) == ("xin tʃaː", "spk:Adam", "style:Adam")
    assert kw == {"steps": 16, "cfg": 3.0, "sway": 0.0, "speed": 1.2, "seed": 3}
    with pytest.raises(Unsupported):
        LocalHandle("turbo", nano).infer_phonemes("x")
