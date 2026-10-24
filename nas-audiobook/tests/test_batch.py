import numpy as np
import pytest

from app.engine_config import EngineConfigStore, EngineConfig
from app.fleet import WorkerConfig
from app.model import Voice
from app.remote import RemoteHandle
from app.tts import MAX_BATCH, ModelPool, VieneuEngine
from conftest import FakeBackend
from test_engine import api
from test_worker import TOKEN, auth, make_worker, remote_pool

SPEC = Voice("nano", "Mỹ Duyên", 0.7, True)


def test_worker_answers_several_texts_in_one_request_with_each_length(tmp_path):
    c, pool, backend = make_worker(tmp_path)
    pool.get("nano")
    texts = ["Xin chào", "Các bạn", "Tạm biệt nhé"]
    r = c.post("/synthesize_batch", json={"model": "nano", "texts": texts, "voice": "Mỹ Duyên", "temperature": 0.7},
               headers=auth())
    assert r.status_code == 200 and r.headers["x-sample-rate"] == "24000"
    counts = [int(n) for n in r.headers["x-sample-counts"].split(",")]
    assert counts == [int(24000 * 0.01 * len(t)) for t in texts] and len(r.content) == 2 * sum(counts)
    assert backend.batch_calls == [("nano", texts, "Mỹ Duyên", 0.7)]


def test_worker_batch_checks_its_limits_and_refuses_phone_only_workers(tmp_path):
    c, _, _ = make_worker(tmp_path)
    assert c.post("/synthesize_batch", json={"model": "nano", "texts": []}, headers=auth()).status_code == 400
    too_many = {"model": "nano", "texts": ["a"] * (MAX_BATCH + 1)}
    assert c.post("/synthesize_batch", json=too_many, headers=auth()).status_code == 400
    long = {"model": "nano", "texts": ["x" * 6001]}
    assert c.post("/synthesize_batch", json=long, headers=auth()).status_code == 400
    assert c.post("/synthesize_batch", json={"model": "huge", "texts": ["a"]}, headers=auth()).status_code == 400
    assert c.get("/health", headers=auth()).json()["capabilities"]["batch"] is True


def test_remote_batch_gives_the_same_audio_as_one_text_at_a_time_in_one_request(tmp_path):
    c, _, fake = make_worker(tmp_path)
    pool, _ = remote_pool(tmp_path, c)
    texts = ["Xin chào các bạn", "Hôm nay trời đẹp", "Hẹn gặp lại"]
    together = pool.synthesize_many("nano", texts, "Mỹ Duyên", 0.8)
    assert len(fake.batch_calls) == 1 and fake.batch_calls[0][1] == texts
    apart = [pool.synthesize("nano", t, "Mỹ Duyên", 0.8) for t in texts]
    assert [len(a) for a in together] == [len(a) for a in apart]
    assert all(np.abs(a - b).max() < 1e-3 for a, b in zip(together, apart))


def test_remote_batch_splits_long_lists_into_requests_of_the_maximum_size(tmp_path):
    c, _, fake = make_worker(tmp_path)
    pool, _ = remote_pool(tmp_path, c)
    handle = pool.get("nano")
    audios = handle.infer_batch([f"câu số {i}" for i in range(MAX_BATCH + 5)], voice="Mỹ Duyên")
    assert [len(texts) for _m, texts, _v, _t in fake.batch_calls] == [MAX_BATCH, 5]
    assert len(audios) == MAX_BATCH + 5


def test_a_worker_without_the_batch_capability_gets_one_text_per_request(tmp_path):
    c, _, fake = make_worker(tmp_path)
    backend = remote_pool(tmp_path, c)[1]
    handle = RemoteHandle(backend, "nano", {"synthesize": True, "clone": True})
    audios = handle.infer_batch(["một", "hai"], voice="Mỹ Duyên")
    assert fake.batch_calls == [] and [t for _m, t, _v, _temp in fake.calls] == ["một", "hai"]
    assert len(audios) == 2


def test_a_worker_that_answers_the_wrong_number_of_texts_is_an_error(tmp_path):
    from app.remote import WorkerError

    c, _, _ = make_worker(tmp_path)
    backend = remote_pool(tmp_path, c)[1]
    handle = RemoteHandle(backend, "nano", {"synthesize": True, "clone": True, "batch": True})

    class Short:
        status_code = 200
        headers = {"x-sample-rate": "24000", "x-sample-counts": "3"}
        content = b"\x00\x00" * 3

    backend.request = lambda *_a, **_k: Short()
    with pytest.raises(WorkerError):
        handle.infer_batch(["một", "hai"])


def test_engine_sends_its_batch_size_and_local_batches_run_in_one_call():
    backend = FakeBackend()
    pool = ModelPool(backend)
    engine = VieneuEngine(pool, SPEC, batch=3)
    audios = engine.synthesize_many(["một", "hai", "ba"], dialog=False)
    assert engine.batch == 3 and len(audios) == 3
    assert backend.batch_calls == [("nano", ["một", "hai", "ba"], "Mỹ Duyên", 0.7)]


def test_dialogue_batches_use_the_dialogue_voice(tmp_path):
    backend = FakeBackend()
    engine = VieneuEngine(ModelPool(backend), Voice("nano", "Mỹ Duyên", 0.7, True, "Anh Khôi"), batch=2)
    engine.synthesize_many(["nói", "lớn"], dialog=True)
    assert backend.batch_calls[0][2] == "Anh Khôi"


def test_batch_size_is_saved_per_machine_and_checked(settings):
    c, ctl = api(settings)
    saved = c.put("/api/engine", json={"local_enabled": True, "local_batch": 4, "workers": [
        {"name": "Laptop", "url": "10.0.0.7:8780", "token": "t", "batch": 16}]})
    assert saved.status_code == 200
    body = saved.json()
    assert body["local"]["batch"] == 4 and body["workers"][0]["batch"] == 16
    assert {n.id: n.batch for n in ctl.nodes()} == {"local": 4, body["workers"][0]["id"]: 16}
    assert c.get("/api/workers").json()[0]["batch"] == 4
    assert c.put("/api/engine", json={"local_enabled": True, "local_batch": 0, "workers": []}).status_code == 422
    assert c.put("/api/engine", json={"local_enabled": True, "workers": [
        {"name": "x", "url": "10.0.0.8:8780", "batch": MAX_BATCH + 1}]}).status_code == 422


def test_batch_size_survives_a_restart_and_older_files_default_to_one(settings, tmp_path):
    store = EngineConfigStore(settings.engine_config)
    store.save(EngineConfig(True, (WorkerConfig("w", "W", "http://w:8780", None, True, 8),), 2))
    loaded = EngineConfigStore(settings.engine_config).load(EngineConfig())
    assert loaded.local_batch == 2 and loaded.workers[0].batch == 8
    settings.engine_config.write_text('{"local_enabled": true, "workers": [{"id": "w", "name": "W", "url": "http://w:8780"}]}')
    assert EngineConfigStore(settings.engine_config).load(EngineConfig()).workers[0].batch == 1
