from fastapi.testclient import TestClient

from app.api import create_app
from app.params import PARAMS
from app.presets import PresetError, PresetStore


def store_at(tmp_path):
    return PresetStore(tmp_path / "config" / "presets.json")


def test_a_preset_is_created_then_updated_by_name_without_regard_to_capitals(tmp_path):
    presets = store_at(tmp_path)
    first = presets.save(name="  Giọng  kể ", model="nano", voice="Mỹ Duyên", dialog_voice=None,
                         params={"speed": 1.2}, read_titles=True)
    again = presets.save(name="giọng kể", model="turbo", voice="Anh Khôi", dialog_voice="Mỹ Duyên",
                         params={"temperature": 0.9, "top_k": 40}, read_titles=False)
    assert again.id == first.id and again.name == "giọng kể"
    assert [(p.model, p.voice, p.dialog_voice, p.params["temperature"], p.params["top_k"], p.read_titles)
            for p in presets.list()] == [("turbo", "Anh Khôi", "Mỹ Duyên", 0.9, 40, False)]


def test_updating_by_id_can_rename_but_not_onto_another_preset(tmp_path):
    presets = store_at(tmp_path)
    a = presets.save(name="A", model="nano", voice=None, dialog_voice=None, params={}, read_titles=True)
    presets.save(name="B", model="nano", voice=None, dialog_voice=None, params={}, read_titles=True)
    renamed = presets.save(id=a.id, name="Alpha", model="nano", voice=None, dialog_voice=None, params={},
                           read_titles=True)
    assert renamed.id == a.id and [p.name for p in presets.list()] == ["Alpha", "B"]
    try:
        presets.save(id=a.id, name="b", model="nano", voice=None, dialog_voice=None, params={}, read_titles=True)
    except PresetError as exc:
        assert "already" in str(exc)
    else:
        raise AssertionError("a preset must not take another preset's name")


def test_names_are_required_and_limited(tmp_path):
    presets = store_at(tmp_path)
    for name in ("", "   ", "x" * 61):
        try:
            presets.save(name=name, model="nano", voice=None, dialog_voice=None, params={}, read_titles=True)
        except PresetError:
            continue
        raise AssertionError(f"{name!r} should not be a preset name")


def test_presets_survive_a_restart_and_delete_removes_them(tmp_path):
    presets = store_at(tmp_path)
    keep = presets.save(name="Keep", model="english", voice="F2", dialog_voice=None, params={}, read_titles=True)
    gone = presets.save(name="Gone", model="nano", voice=None, dialog_voice=None, params={}, read_titles=True)
    reopened = store_at(tmp_path)
    assert [p.name for p in reopened.list()] == ["Keep", "Gone"]
    assert reopened.delete(gone.id) is True and reopened.delete(gone.id) is False
    assert [p.id for p in store_at(tmp_path).list()] == [keep.id]


def test_a_missing_or_damaged_file_is_an_empty_list(tmp_path):
    presets = store_at(tmp_path)
    assert presets.list() == []
    presets.path.parent.mkdir(parents=True)
    presets.path.write_text("not json", encoding="utf8")
    assert presets.list() == []


def test_presets_api_saves_lists_and_deletes(settings, store):
    c = TestClient(create_app(settings, store, None))
    body = {"name": "Kể chuyện", "model": "turbo", "voice": "Mỹ Duyên", "dialog_voice": "Anh Khôi",
            "params": {"temperature": 0.6, "top_p": 0.9}, "read_titles": False}
    created = c.post("/api/presets", json=body).json()
    assert created["name"] == "Kể chuyện" and created["id"]
    updated = c.post("/api/presets", json={**body, "id": created["id"], "params": {"temperature": 0.9}}).json()
    assert updated["id"] == created["id"] and updated["params"]["temperature"] == 0.9
    assert created["params"]["temperature"] == 0.6 and created["params"]["top_p"] == 0.9
    assert set(updated["params"]) == {p.key for p in PARAMS["turbo"]}  # every setting of the model is kept
    assert c.get("/api/presets").json() == [updated]
    assert c.delete(f"/api/presets/{created['id']}").status_code == 204
    assert c.get("/api/presets").json() == []
    assert c.delete(f"/api/presets/{created['id']}").status_code == 404


def test_presets_api_checks_the_model_name_the_settings_and_the_id(settings, store):
    c = TestClient(create_app(settings, store, None))
    base = {"name": "P", "model": "nano", "voice": None, "dialog_voice": None, "params": {}, "read_titles": True}
    assert c.post("/api/presets", json={**base, "model": "huge"}).status_code == 422
    assert c.post("/api/presets", json={**base, "params": {"speed": 9}}).status_code == 422
    assert c.post("/api/presets", json={**base, "params": {"temperature": 0.8}}).status_code == 422  # nano has none
    assert c.post("/api/presets", json={**base, "id": "missing"}).status_code == 404
