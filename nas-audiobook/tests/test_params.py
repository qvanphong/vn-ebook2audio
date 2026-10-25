"""Each model has its own settings: they are checked, saved with jobs, presets and library audio, and reach the engine."""
import hashlib
import json
import sqlite3

import pytest

from app import params
from app.jobs import JobStore
from app.library import Library
from app.model import State, Voice
from app.params import PARAMS, ParamError, engine_kwargs, resolve
from app.remote import RemoteHandle, WorkerError
from app.worker import options_for, Synthesis
from conftest import FakeEngine
from test_flow import make_client, three_chapter_epub, upload
from test_library_api import convert_and_run
from test_worker import auth, make_worker


LONG_TEXT = "một hai ba bốn năm sáu bảy tám chín mười " * 3  # about 1.2 seconds from the fake model


def sha(raw):
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


# --- the table -------------------------------------------------------------------------------

def test_every_model_lists_its_own_settings_and_every_default_is_valid():
    assert {m: [p.key for p in table] for m, table in PARAMS.items()} == {
        "nano": ["speed", "steps", "cfg", "sway", "seed", "max_chars"],
        "turbo": ["speed", "temperature", "top_k", "top_p", "repetition_penalty", "repetition_window", "max_new_frames", "max_chars"],
        "english": ["speed", "total_steps", "silence_duration", "max_chunk_length"]}
    for model in PARAMS:
        assert dict(resolve(model)) == {p.key: p.default for p in PARAMS[model]}


def test_values_are_checked_against_the_model_and_nothing_is_skipped_quietly():
    assert dict(resolve("turbo", {"temperature": 1.1, "top_k": 40}))["temperature"] == 1.1
    with pytest.raises(ParamError, match="no setting 'temperature'"):
        resolve("nano", {"temperature": 0.9})
    with pytest.raises(ParamError, match="between"):
        resolve("turbo", {"temperature": 5})
    with pytest.raises(ParamError, match="whole number"):
        resolve("turbo", {"top_k": 2.5})
    with pytest.raises(ParamError, match="must be a number"):
        resolve("turbo", {"top_p": "high"})
    with pytest.raises(ParamError, match="unknown model"):
        resolve("huge")


def test_an_empty_value_means_the_default_and_only_settings_with_a_value_reach_the_engine():
    values = resolve("nano", {"steps": "", "speed": None, "seed": 7}, {"speed": 1.1})
    assert dict(values)["speed"] == 1.1 and dict(values)["steps"] is None
    assert engine_kwargs(values) == {"speed": 1.1, "sway": 0.0, "seed": 7, "max_chars": 140}


def test_records_from_before_models_had_settings_keep_turbos_temperature_only():
    assert params.of_record({"model": "turbo", "temperature": 0.6})["temperature"] == 0.6
    assert params.of_record({"model": "nano", "temperature": 0.6}) == dict(resolve("nano"))
    assert params.of_record({"model": "nano", "params": {"speed": 1.3, "gone": 1}})["speed"] == 1.3


# --- what counts as the same voice -------------------------------------------------------------

def test_audio_made_before_models_had_settings_still_counts_as_the_same_voice():
    assert Voice("turbo", "A", resolve("turbo", {"temperature": 0.9}), True).signature() == sha("turbo|A|None|0.9|1")
    assert Voice("nano", "A", resolve("nano"), False, "B").signature() == sha("nano|A|B|0.8|0")
    assert Voice("english", "F1", resolve("english"), True).signature() == sha("english|F1|None|0.8|1")


def test_changing_any_setting_makes_a_different_voice():
    base = Voice("nano", "A", resolve("nano"), True).signature()
    assert Voice("nano", "A", resolve("nano", {"speed": 1.2}), True).signature() != base
    assert Voice("nano", "A", resolve("nano", {"seed": 3}), True).signature() != base
    assert Voice("turbo", "A", resolve("turbo", {"top_k": 30}), True).signature() != Voice("turbo", "A", resolve("turbo"), True).signature()


# --- the HTTP side -------------------------------------------------------------------------------

def test_the_page_can_ask_for_every_models_settings_with_their_starting_values(settings, store):
    c, _ = make_client(settings, store)
    table = c.get("/api/params").json()
    assert list(table) == ["nano", "turbo", "english"]
    turbo = {p["key"]: p for p in table["turbo"]}
    assert turbo["temperature"]["default"] == settings.temperature and turbo["top_k"]["max"] == 200
    assert {p["key"] for p in table["english"]} == {"speed", "total_steps", "silence_duration", "max_chunk_length"}
    assert "voice_params" not in c.get("/api/defaults").json()


def test_a_conversion_keeps_its_settings_on_the_job_and_on_the_audio_it_makes(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    job, engine = convert_and_run(c, settings, store, path, {"model": "nano", "voice_params": {"speed": 1.25, "seed": 9}})
    assert job["params"]["speed"] == 1.25 and job["params"]["seed"] == 9 and job["params"]["max_chars"] == 140
    assert store.get(job["id"]).spec.params == resolve("nano", {"speed": 1.25, "seed": 9})
    preview = c.post(f"/api/uploads/{upload(c, path)}/preview", json={"processor": "epub-toc"}).json()
    made = preview["chapters"][1]["audio"][0]
    assert made["params"]["speed"] == 1.25 and made["params"]["seed"] == 9


def test_different_settings_are_kept_as_separate_audio(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    path = three_chapter_epub(toc_epub)
    convert_and_run(c, settings, store, path, {"selection": [1], "model": "nano", "voice_params": {"speed": 1.25}})
    convert_and_run(c, settings, store, path, {"selection": [1], "model": "nano", "voice_params": {"speed": 0.9}})
    md5 = c.get(f"/api/uploads/{upload(c, path)}").json()["md5"]
    groups = c.get(f"/api/library/{md5}").json()["groups"]
    assert sorted(g["params"]["speed"] for g in groups) == [0.9, 1.25]


def test_a_setting_the_model_does_not_have_is_refused(settings, store, toc_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, three_chapter_epub(toc_epub))
    bad = c.post(f"/api/uploads/{uid}/convert", json={"model": "nano", "voice_params": {"temperature": 0.7}})
    assert bad.status_code == 422 and "no setting 'temperature'" in bad.json()["detail"]
    assert c.post(f"/api/uploads/{uid}/convert", json={"model": "turbo", "voice_params": {"top_p": 7}}).status_code == 422
    assert c.get("/api/voice-sample", params={"model": "nano", "voice_params": "[1]"}).status_code == 422
    assert c.get("/api/voice-sample", params={"model": "nano", "voice_params": '{"top_k": 3}'}).status_code == 422
    assert store.list() == []


def test_the_quick_path_takes_settings_as_json(settings, store):
    c, _ = make_client(settings, store)
    body = {"model": "turbo", "voice_params": json.dumps({"temperature": 1.2, "top_p": 0.8})}
    job = c.post("/api/jobs", files={"file": ("x.txt", "Xin chào, đây là một câu tiếng Việt.".encode())}, data=body).json()
    assert job["params"]["temperature"] == 1.2 and job["params"]["top_p"] == 0.8


def test_a_voice_sample_is_made_with_the_chosen_settings(settings, store):
    c, pool = make_client(settings, store)
    r = c.get("/api/voice-sample", params={"model": "nano", "voice_params": json.dumps({"speed": 1.4})})
    assert r.status_code == 200 and pool.synth_calls[-1][3] == {"speed": 1.4, "sway": 0.0, "max_chars": 140}


# --- saved before ------------------------------------------------------------------------------

def test_a_job_database_from_before_keeps_each_jobs_temperature_where_it_counted(tmp_path):
    db = tmp_path / "old.sqlite"
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE jobs (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
      state TEXT NOT NULL, model TEXT NOT NULL, voice TEXT, dialog_voice TEXT, temperature REAL NOT NULL,
      read_titles INTEGER NOT NULL, chapters_done INTEGER NOT NULL DEFAULT 0, chapters_total INTEGER NOT NULL DEFAULT 0,
      output TEXT, error TEXT, cancel INTEGER NOT NULL DEFAULT 0, plan TEXT, chapters TEXT, book_md5 TEXT,
      created REAL NOT NULL, updated REAL NOT NULL)""")
    for job_id, model in (("t", "turbo"), ("n", "nano")):
        con.execute("INSERT INTO jobs (id, source, state, model, temperature, read_titles, created, updated)"
                    " VALUES (?,'/b.epub','done',?,0.6,1,1,1)", (job_id, model))
    con.commit(); con.close()
    store = JobStore(db)
    assert dict(store.get("t").spec.params)["temperature"] == 0.6 and store.get("t").public()["params"]["top_k"] == 25
    assert store.get("n").spec.params == resolve("nano") and store.get("n").state is State.DONE
    assert JobStore(db).get("t").spec.params == store.get("t").spec.params  # opening it again changes nothing


def test_a_library_manifest_and_a_presets_file_from_before_still_load(tmp_path):
    from app.presets import PresetStore

    md5 = "a" * 32
    library = Library(tmp_path / "lib")
    library.ensure_book(md5, title="T", author="A", language="vi", source_name="b.epub", cover=None)
    (library.dir(md5) / "c.m4a").write_bytes(b"x")
    manifest = json.loads((library.dir(md5) / "book.json").read_text(encoding="utf8"))
    manifest["audio"] = [{"file": "c.m4a", "index": 0, "title": "c", "text_hash": "h", "chars": 1, "seconds": 1.0, "bytes": 1,
                          "model": "turbo", "voice": None, "dialog_voice": None, "temperature": 0.7, "read_titles": True,
                          "voice_sig": "s", "processor": "auto", "created": 1.0}]
    (library.dir(md5) / "book.json").write_text(json.dumps(manifest), encoding="utf8")
    assert dict(library.entries(md5)[0].params)["temperature"] == 0.7

    presets = PresetStore(tmp_path / "presets.json")
    presets.path.write_text(json.dumps([{"id": "p", "name": "Old", "model": "turbo", "voice": None, "dialog_voice": None,
                                         "temperature": 0.5, "read_titles": True}]), encoding="utf8")
    assert presets.list()[0].params["temperature"] == 0.5 and presets.list()[0].params["top_k"] == 25


# --- workers -------------------------------------------------------------------------------------

def test_a_worker_takes_every_setting_its_model_has_and_refuses_the_rest(tmp_path):
    c, pool, backend = make_worker(tmp_path)
    pool.get("turbo")
    every = {"temperature": 1.1, "top_k": 40, "top_p": 0.9, "repetition_penalty": 1.3, "repetition_window": 32,
             "max_new_frames": 400, "max_chars": 200}
    r = c.post("/synthesize", json={"model": "turbo", "text": "x", "params": every}, headers=auth())
    assert r.status_code == 200 and backend.calls[-1][3] == every
    r = c.post("/synthesize_batch", json={"model": "turbo", "texts": ["x", "y"], "params": every}, headers=auth())
    assert r.status_code == 200 and backend.batch_calls[-1][3] == every
    for bad in ({"cfg": 1.0}, {"top_k": 0}, {"top_k": 2.5}):
        r = c.post("/synthesize", json={"model": "turbo", "text": "x", "params": bad}, headers=auth())
        assert r.status_code == 400, bad


def test_a_worker_still_reads_the_temperature_an_older_server_sends():
    assert options_for(Synthesis(model="turbo", text="x", temperature=0.5))["temperature"] == 0.5
    assert "temperature" not in options_for(Synthesis(model="nano", text="x", temperature=0.5))


def test_the_server_will_not_pass_settings_to_a_worker_too_old_to_take_them():
    handle = RemoteHandle(None, "nano", {"synthesize": True, "clone": True})
    assert handle._settings({"speed": 1.0, "max_chars": 140}) == {"temperature": 0.8}
    with pytest.raises(WorkerError, match="too old.*speed"):
        handle._settings({"speed": 1.2})
    new = RemoteHandle(None, "nano", {"synthesize": True, "clone": True, "params": True})
    assert new._settings({"speed": 1.2}) == {"params": {"speed": 1.2}}
    old_turbo = RemoteHandle(None, "turbo", {"synthesize": True, "clone": True})
    assert old_turbo._settings({"temperature": 1.1}) == {"temperature": 1.1}


# --- speed ---------------------------------------------------------------------------------------

def test_a_clip_is_sped_up_without_changing_its_pitch():
    import numpy as np

    from app.stretch import change_speed

    rate = 24000
    tone = (0.5 * np.sin(2 * np.pi * 440 * np.arange(rate * 2) / rate)).astype(np.float32)
    assert change_speed(tone, rate, 1.0) is tone
    for speed in (0.5, 1.25, 2.0):
        out = change_speed(tone, rate, speed)
        assert abs(len(out) / len(tone) - 1 / speed) < 0.05
        peak = np.argmax(np.abs(np.fft.rfft(out))) * rate / len(out)
        assert abs(peak - 440) < 15, (speed, peak)


def test_turbo_has_no_speed_of_its_own_so_the_pool_applies_it_and_the_other_models_do_it_natively(tmp_path):
    from app.tts import ModelPool
    from conftest import FakeBackend

    backend = FakeBackend()
    pool = ModelPool(backend)
    normal = pool.synthesize("turbo", LONG_TEXT, None, {"temperature": 0.9})
    fast = pool.synthesize("turbo", LONG_TEXT, None, {"temperature": 0.9, "speed": 2.0})
    assert abs(len(fast) / len(normal) - 0.5) < 0.05
    assert backend.calls[-1][3] == {"temperature": 0.9}  # the engine is not given a speed it does not have
    batch = pool.synthesize_many("turbo", [LONG_TEXT, LONG_TEXT + " năm sáu bảy"], None, {"speed": 2.0})
    assert [len(a) for a in batch] == [pytest.approx(len(pool.synthesize("turbo", t, None, {})) / 2, rel=0.05)
                                       for t in (LONG_TEXT, LONG_TEXT + " năm sáu bảy")]
    pool.synthesize("nano", "một hai", None, {"speed": 1.5})
    assert backend.calls[-1][3] == {"speed": 1.5}  # Nano takes it itself


def test_a_worker_applies_turbos_speed_too(tmp_path):
    c, pool, backend = make_worker(tmp_path)
    pool.get("turbo")
    one = c.post("/synthesize", json={"model": "turbo", "text": LONG_TEXT, "params": {"speed": 1.0}}, headers=auth())
    two = c.post("/synthesize", json={"model": "turbo", "text": LONG_TEXT, "params": {"speed": 2.0}}, headers=auth())
    assert two.status_code == 200 and abs(len(two.content) / len(one.content) - 0.5) < 0.05
    assert c.post("/synthesize", json={"model": "turbo", "text": "x", "params": {"speed": 3}}, headers=auth()).status_code == 400
