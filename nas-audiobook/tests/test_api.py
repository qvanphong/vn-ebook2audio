from fastapi.testclient import TestClient

from app.api import create_app
from app.jobs import Runner
from app import params
from app.model import State
from app.tts import CloneError, ModelLoading, VoiceList
from app.watcher import scan_inbox
from conftest import FakeEngine


class FakePool:
    def __init__(self):
        self.clones: dict[str, bytes] = {}
        self.synth_calls: list[tuple] = []

    def ensure(self, model):
        if self.loading:
            raise ModelLoading(model, None if self.loading == "wait" else self.loading)

    def synthesize(self, model, text, voice, options):
        import numpy as np

        self.synth_calls.append((model, text, voice, options))
        return (0.2 * np.sin(np.linspace(0, 200 * np.pi, 24000))).astype(np.float32)

    loading: str | None = None

    def voices(self, model):
        if model not in ("nano", "turbo"):
            raise ValueError(model)
        if self.loading:
            raise ModelLoading(model, None if self.loading == "wait" else self.loading)
        return VoiceList([{"id": "Mỹ Duyên", "label": model, "cloned": False}] + [
            {"id": n, "label": n, "cloned": True} for n in self.clones], True)

    def add_clone(self, name, upload, default_model):
        if name == "Mỹ Duyên":
            raise CloneError("built-in")
        self.clones[name] = upload.read_bytes()

    def remove_clone(self, name):
        return self.clones.pop(name, None) is not None


def client(settings, store, pool=None):
    return TestClient(create_app(settings, store, pool or FakePool()))


def test_upload_runs_to_done_and_lists_job(settings, store, make_epub):
    c = client(settings, store)
    with open(make_epub(), "rb") as f:
        r = c.post("/api/jobs", files={"file": ("book.epub", f)}, data={"model": "nano", "voice": "Mỹ Duyên"})
    assert r.status_code == 201 and r.json()["voice"] == "Mỹ Duyên"
    Runner(settings, store, lambda _v: FakeEngine()).run_one(store.claim_next())
    job = c.get(f"/api/jobs/{r.json()['id']}").json()
    assert job["state"] == "done" and job["output"].endswith(job["book_md5"])
    assert len(list(__import__("pathlib").Path(job["output"]).glob("*.m4a"))) == 2
    assert [j["id"] for j in c.get("/api/jobs").json()] == [job["id"]]


def test_rejects_unsupported_format_and_unknown_model(settings, store):
    c = client(settings, store)
    assert c.post("/api/jobs", files={"file": ("x.exe", b"x")}).status_code == 415
    assert c.post("/api/jobs", files={"file": ("x.txt", b"x")}, data={"model": "huge"}).status_code == 400


def test_cancel_retry_delete_rules(settings, store, make_epub):
    c = client(settings, store)
    with open(make_epub(), "rb") as f:
        jid = c.post("/api/jobs", files={"file": ("book.epub", f)}).json()["id"]
    assert c.delete(f"/api/jobs/{jid}").status_code == 409
    assert c.post(f"/api/jobs/{jid}/cancel").json()["state"] == "cancelled"
    assert c.post(f"/api/jobs/{jid}/retry").json()["state"] == "queued"
    store.update(jid, state=State.ERROR)
    assert c.delete(f"/api/jobs/{jid}").status_code == 204
    assert c.get(f"/api/jobs/{jid}").status_code == 404


def test_voices_defaults_health_and_index(settings, store):
    c = client(settings, store)
    assert c.get("/healthz").json() == {"ok": True}
    assert c.get("/api/voices?model=turbo").json()["voices"][0]["label"] == "turbo"
    assert c.get("/api/voices?model=huge").status_code == 400
    assert ".epub" in c.get("/api/defaults").json()["formats"]
    assert "Ebook to audiobook" in c.get("/").text


def test_inbox_scan_queues_settled_supported_files_once(settings, store, make_epub):
    src = make_epub("drop.epub")
    (settings.inbox / "sub").mkdir()
    (settings.inbox / "sub" / "drop.epub").write_bytes(src.read_bytes())
    (settings.inbox / "notes.xyz").write_text("x")
    assert scan_inbox(settings, store) == 1
    assert scan_inbox(settings, store) == 0
    assert [p.name for p in settings.books.glob("*/*")] == ["drop.epub"] and (settings.inbox / "notes.xyz").exists()
    assert [j.title for j in store.list()] == ["drop"]


def test_inbox_scan_waits_for_file_to_settle(settings, store, make_epub):
    import dataclasses
    waiting = dataclasses.replace(settings, settle_seconds=60.0)
    (waiting.inbox / "a.epub").write_bytes(make_epub().read_bytes())
    assert scan_inbox(waiting, store) == 0


def test_omitted_form_fields_fall_back_to_server_defaults(settings, store):
    c = client(settings, store)
    job = c.post("/api/jobs", files={"file": ("x.txt", "Xin chào, đây là một câu tiếng Việt.".encode())}).json()
    assert (job["model"], job["voice"], job["params"], job["read_titles"]) == (
        settings.model, settings.voice, dict(params.resolve(settings.model)), settings.read_titles)
    off = c.post("/api/jobs", files={"file": ("y.txt", b"x")}, data={"read_titles": "false"}).json()
    assert off["read_titles"] is False


def test_threads_default_to_available_cpus_and_can_be_overridden(monkeypatch):
    from app.config import Settings, available_cpus

    monkeypatch.delenv("TTS_THREADS", raising=False)
    assert Settings.from_env().threads == available_cpus() >= 1
    monkeypatch.setenv("TTS_THREADS", "3")
    assert Settings.from_env().threads == 3


def test_clone_upload_list_and_delete(settings, store):
    c = client(settings, store)
    assert c.post("/api/voices", data={"name": "Giọng của tôi"}, files={"file": ("a.wav", b"RIFF")}).status_code == 201
    assert [v["id"] for v in c.get("/api/voices").json()["voices"] if v["cloned"]] == ["Giọng của tôi"]
    assert c.post("/api/voices", data={"name": "Mỹ Duyên"}, files={"file": ("a.wav", b"RIFF")}).status_code == 400
    assert c.delete("/api/voices/Giọng của tôi").status_code == 204
    assert c.delete("/api/voices/Giọng của tôi").status_code == 404


def test_dialog_voice_is_stored_on_the_job(settings, store):
    c = client(settings, store)
    job = c.post("/api/jobs", files={"file": ("x.txt", b"x")}, data={"dialog_voice": "Mỹ Duyên"}).json()
    assert job["dialog_voice"] == "Mỹ Duyên"


def test_voices_answers_503_while_the_model_loads_or_after_it_fails(settings, store):
    pool = FakePool()
    c = client(settings, store, pool)
    pool.loading = "wait"
    r = c.get("/api/voices?model=nano")
    assert r.status_code == 503 and r.json()["detail"]["loading"] is True
    pool.loading = "OSError: no network"
    r = c.get("/api/voices?model=nano")
    assert r.status_code == 503 and r.json()["detail"] == {"loading": False, "message": "OSError: no network"}
