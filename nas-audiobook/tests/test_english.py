"""English books: the English model (Supertonic), picked automatically for an English book, with its own voices."""
import sys
import time
import types

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import samples
from app.api import create_app
from app.config import Settings
from app.engine_config import EngineConfigStore, EngineController, EngineConfig
from app.jobs import Runner
from app.model import Voice
from app.remote import RemoteBackend, WorkerError
from app.text import split_sentences
from app.tts import (MODEL_MODES, SAMPLE_RATES, LocalBackend, ModelPool, Unsupported, VoiceMissing,
                     SupertonicHandle)
from app.watcher import scan_inbox
from conftest import FakeEngine
from test_api import FakePool
from test_flow import upload
from test_worker import TOKEN, make_worker

ENGLISH = [("Chapter 1", ["The old lighthouse stood at the edge of the cliff, its paint peeling after a hundred winters.",
                          "Mr. Alder had kept its light for forty-one years, and nobody had ever thanked him for it.",
                          " ".join(["The wind carried the smell of salt across the empty bay."] * 6)])]
VOICES = ["F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5"]


class FakeSupertonic:
    """Stands in for supertonic.TTS: records calls, answers with a second of silence per call."""

    def __init__(self, **kw):
        self.kw, self.calls, self.voice_style_names = kw, [], list(VOICES)

    def get_voice_style(self, name):
        return f"style:{name}"

    def synthesize(self, text, voice_style, total_steps=8, speed=1.05, max_chunk_length=None, silence_duration=0.3,
                   lang=None, verbose=False):
        self.calls.append((text, voice_style, total_steps, lang))
        return np.zeros((1, 44100), dtype=np.float32), np.array([1.0])


@pytest.fixture
def fake_module(monkeypatch):
    module = types.ModuleType("supertonic")
    module.TTS = FakeSupertonic
    monkeypatch.setitem(sys.modules, "supertonic", module)
    return module


# --- the model ---------------------------------------------------------------------------

def test_the_english_model_is_listed_with_its_own_sample_rate():
    assert "english" in MODEL_MODES and SAMPLE_RATES["english"] == 44100


def test_the_local_backend_loads_it_with_the_chosen_thread_count_and_names_ten_voices(fake_module):
    handle = LocalBackend(3, "fp32").load("english")
    assert isinstance(handle, SupertonicHandle) and handle._tts.kw == {"intra_op_num_threads": 3}
    labels = dict((vid, label) for label, vid in handle.list_preset_voices())
    assert list(labels) == VOICES and labels["F1"] == "F1 — Female" and labels["M5"] == "M5 — Male"
    assert handle.voice_ids() == VOICES and handle.can_clone is False


def test_it_speaks_english_with_the_chosen_voice_and_ignores_the_vietnamese_expressiveness_setting(fake_module):
    handle = LocalBackend(1, "fp32").load("english")
    audio = handle.infer("Hello there.", voice="M2", temperature=1.4)
    assert audio.dtype == np.float32 and audio.shape == (44100,)
    assert handle._tts.calls == [("Hello there.", "style:M2", 8, "en")]
    handle.infer("And without a voice.")
    assert handle._tts.calls[-1][1] == "style:F1"


def test_a_voice_it_does_not_have_is_refused_by_name_and_cloning_is_unsupported(fake_module):
    handle = LocalBackend(1, "fp32").load("english")
    with pytest.raises(ValueError, match="no voice 'Mỹ Duyên'"):
        handle.infer("Hi.", voice="Mỹ Duyên")
    with pytest.raises(Unsupported, match="cannot clone"):
        handle.add_voice("mine", "ref.wav")
    handle.remove_voice("mine")  # nothing to remove, and no error


def test_without_the_package_the_error_says_what_to_install(monkeypatch):
    monkeypatch.setitem(sys.modules, "supertonic", None)
    with pytest.raises(RuntimeError, match="pip install supertonic"):
        LocalBackend(1, "fp32").load("english")


class SupertonicBackend:
    def load(self, model):
        return SupertonicHandle(FakeSupertonic())


def test_cloned_voices_never_show_up_in_the_english_list_even_before_it_loads(tmp_path):
    from app.tts import CloneStore
    from conftest import write_wav

    clones = CloneStore(tmp_path / "c")
    clones.add("Giọng tôi", write_wav(tmp_path / "ref.wav"))
    cache = tmp_path / "v.json"
    cache.write_text('{"english": [{"id": "F1", "label": "F1 — Female", "cloned": false}]}', encoding="utf8")
    pool = ModelPool(SupertonicBackend(), clones, cache)
    assert pool.can_clone("english") is False
    assert [v["id"] for v in pool.voices("english").voices] == ["F1"]
    pool.get("english")
    for _ in range(200):  # the list is refreshed by the loading thread a moment after the model is ready
        if pool.voices("english").ready:
            break
        time.sleep(0.01)
    assert [v["id"] for v in pool.voices("english").voices] == VOICES


def test_a_vietnamese_voice_is_caught_before_a_book_starts_on_the_english_model():
    pool = ModelPool(SupertonicBackend())
    pool.prepare(Voice("english", "M3", 0.8, True, "F2"))
    with pytest.raises(VoiceMissing, match="'Mỹ Duyên'"):
        pool.prepare(Voice("english", "Mỹ Duyên", 0.8, True))


def test_a_phone_that_only_runs_nano_refuses_the_english_model(tmp_path):
    from app.tts import ModelPool as Pool
    from app.worker import create_worker_app
    from conftest import FakeBackend

    phone = TestClient(create_worker_app(Pool(FakeBackend(), None, tmp_path / "p.json"), "nano", TOKEN, "phonemes"),
                       base_url="http://worker")
    with pytest.raises(WorkerError, match="cannot run the english model"):
        RemoteBackend("http://worker", TOKEN, client=phone, sleep=lambda _s: None).load("english")


def test_a_worker_reports_the_english_model_and_answers_at_44_khz(tmp_path):
    c, pool, _ = make_worker(tmp_path)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    assert c.get("/health", headers=headers).json()["models"]["english"] == {"ready": False}
    pool.get("english")
    r = c.post("/synthesize", json={"model": "english", "text": "Hello"}, headers=headers)
    assert r.status_code == 200 and r.headers["x-sample-rate"] == "44100"


def test_the_machine_status_lists_the_english_model(settings):
    ctl = EngineController(EngineConfigStore(settings.engine_config), EngineConfig(),
                           lambda w: ModelPool(SupertonicBackend(), None, settings.voice_cache))
    assert set(ctl.status()[0]["models"]) == {"nano", "turbo", "english"}


# --- choosing it for an English book --------------------------------------------------------

def test_an_english_book_starts_on_the_english_model_and_a_vietnamese_one_does_not(tmp_path):
    s = Settings(data_dir=tmp_path, voice="Mỹ Duyên", dialog_voice="Anh Khôi")
    assert s.defaults_for("en") == {"model": "english", "voice": None, "dialog_voice": None}
    assert s.defaults_for("en-GB")["model"] == "english"
    assert s.defaults_for("vi") == {"model": "nano", "voice": "Mỹ Duyên", "dialog_voice": "Anh Khôi"}
    assert s.defaults_for(None)["model"] == "nano" and s.defaults_for("fr")["model"] == "nano"


def test_the_english_default_can_be_switched_off_or_given_a_voice(tmp_path, monkeypatch):
    assert Settings(data_dir=tmp_path, english_model=None).defaults_for("en")["model"] == "nano"
    assert Settings(data_dir=tmp_path, english_voice="M1").defaults_for("en")["voice"] == "M1"
    monkeypatch.setenv("TTS_ENGLISH_MODEL", "none")
    assert Settings.from_env().english_model is None
    monkeypatch.setenv("TTS_ENGLISH_MODEL", "turbo")
    monkeypatch.setenv("TTS_ENGLISH_VOICE", "Adam")
    s = Settings.from_env()
    assert (s.english_model, s.english_voice) == ("turbo", "Adam")
    monkeypatch.delenv("TTS_ENGLISH_MODEL")
    monkeypatch.delenv("TTS_ENGLISH_VOICE")
    assert (Settings.from_env().english_model, Settings.from_env().english_voice) == ("english", None)


def client(settings, store):
    return TestClient(create_app(settings, store, FakePool()))


def test_the_page_is_told_what_an_english_book_starts_with(settings, store):
    d = client(settings, store).get("/api/defaults").json()
    assert d["model"] == "nano" and d["english"] == {"model": "english", "voice": None, "dialog_voice": None}


def test_converting_an_english_book_without_a_model_uses_the_english_one(settings, store, make_epub):
    c = client(settings, store)
    uid = upload(c, make_epub("en.epub", ENGLISH, title="The Keeper", language="en"))
    assert c.get(f"/api/uploads/{uid}").json()["language"] == "en"
    job = c.post(f"/api/uploads/{uid}/convert", json={"processor": "auto"}).json()
    assert (job["model"], job["voice"], job["dialog_voice"]) == ("english", None, None)


def test_an_explicit_choice_wins_and_a_default_voice_never_follows_it_to_another_model(settings, store, make_epub):
    c = client(settings, store)
    uid = upload(c, make_epub("en2.epub", ENGLISH, language="en"))
    job = c.post(f"/api/uploads/{uid}/convert", json={"processor": "auto", "model": "nano"}).json()
    assert job["model"] == "nano"
    own = Settings(data_dir=settings.data_dir, voice="Mỹ Duyên", settle_seconds=0.0)
    c2 = TestClient(create_app(own, store, FakePool()))
    uid = upload(c2, make_epub("en3.epub", ENGLISH, language="en"))
    job = c2.post(f"/api/uploads/{uid}/convert", json={"processor": "auto"}).json()
    assert (job["model"], job["voice"]) == ("english", None)
    uid = upload(c2, make_epub("en4.epub", ENGLISH, language="en"))
    job = c2.post(f"/api/uploads/{uid}/convert", json={"processor": "auto", "model": "english", "voice": "F3"}).json()
    assert (job["model"], job["voice"]) == ("english", "F3")


def test_a_vietnamese_book_keeps_the_vietnamese_defaults(settings, store, make_epub):
    c = client(settings, store)
    uid = upload(c, make_epub())
    assert c.post(f"/api/uploads/{uid}/convert", json={"processor": "auto"}).json()["model"] == "nano"


def test_scripts_and_the_inbox_get_the_same_language_choice(settings, store, make_epub):
    c = client(settings, store)
    book = make_epub("en5.epub", ENGLISH, language="en")
    with open(book, "rb") as f:
        assert c.post("/api/jobs", files={"file": ("en5.epub", f)}).json()["model"] == "english"
    (settings.inbox / "dropped.epub").write_bytes(make_epub("en6.epub", ENGLISH, title="Dropped", language="en").read_bytes())
    (settings.inbox / "viet.epub").write_bytes(make_epub("vi.epub").read_bytes())
    assert scan_inbox(settings, store, now=10 ** 12) == 2
    models = {j.title: j.model for j in store.list() if j.title in ("dropped", "viet")}
    assert models == {"dropped": "english", "viet": "nano"}


def test_a_whole_english_book_is_made_with_the_english_voice_and_saved_in_its_own_voice_group(settings, store, make_epub):
    from app.library import Library, md5_file

    book = make_epub("en7.epub", ENGLISH, language="en")
    store.create(book, Voice("english", "M2", 0.8, True))
    Runner(settings, store, lambda _v: FakeEngine()).run_one(store.claim_next())
    (entry,) = Library(settings.library).entries(md5_file(book))
    assert entry.model == "english" and entry.voice == "M2" and entry.voice_label() == "English · M2"


def test_the_sample_sentence_is_english_for_the_english_model():
    assert samples.default_text("english").startswith("Hello") and "Xin chào" not in samples.default_text("english")
    assert samples.default_text("nano").startswith("Xin chào")


# --- English text: abbreviations must not end a sentence ------------------------------------

@pytest.mark.parametrize("text", [
    "Dr. Smith arrived at the old house and knocked twice.",
    "Mr. and Mrs. Alder kept the light for forty years.",
    "He brought tools, e.g. a hammer and a lamp, for the repairs.",
    "They met at 9:30 a.m. in the square before anyone else came.",
    "She studied in the U.S. before moving to the coast.",
    "Prof. Okafor and Capt. Reyes sailed with the St. Mary at dawn.",
])
def test_abbreviations_stay_inside_their_sentence(text):
    assert split_sentences(text) == [text]


def test_real_sentence_ends_still_split():
    assert split_sentences("He left at nine. She stayed until noon. Nobody asked why!") == [
        "He left at nine.", "She stayed until noon.", "Nobody asked why!"]
