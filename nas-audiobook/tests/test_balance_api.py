"""Choosing machines when converting, and the cover shown before converting, through the HTTP API."""
from fastapi.testclient import TestClient

from app.api import create_app
from app.jobs import JobStore
from test_engine import api, worker_entry
from test_flow import make_client, upload

PNG = b"\x89PNG\r\n\x1a\n" + b"png-bytes"


def with_workers(settings, make_epub):
    c, ctl = api(settings)
    ctl.apply(True, [worker_entry("10.0.0.5:8780", name="Pixel"), worker_entry("10.0.0.6:8780", name="Laptop")])
    pixel, laptop = (w.id for w in ctl.config.workers)
    uid = upload(c, make_epub())
    return c, ctl, uid, pixel, laptop


def convert(c, uid, **body):
    return c.post(f"/api/uploads/{uid}/convert", json={"processor": "auto", **body})


def test_a_conversion_remembers_the_machines_it_was_given(settings, make_epub):
    c, ctl, uid, pixel, laptop = with_workers(settings, make_epub)
    r = convert(c, uid, workers=["local", pixel])
    assert r.status_code == 201
    assert JobStore(settings.db).get(r.json()["id"]).plan.workers == ("local", pixel)


def test_without_a_choice_the_job_uses_every_machine_that_is_on(settings, make_epub):
    c, ctl, uid, *_ = with_workers(settings, make_epub)
    r = convert(c, uid)
    assert r.status_code == 201 and JobStore(settings.db).get(r.json()["id"]).plan.workers is None


def test_machines_that_do_not_exist_are_dropped_and_an_empty_choice_is_refused(settings, make_epub):
    c, ctl, uid, pixel, laptop = with_workers(settings, make_epub)
    assert convert(c, uid, workers=["ghost"]).status_code == 422
    assert convert(c, uid, workers=[]).status_code == 422
    r = convert(c, uid, workers=["ghost", laptop])
    assert r.status_code == 201 and JobStore(settings.db).get(r.json()["id"]).plan.workers == (laptop,)


def test_a_server_without_machine_management_cannot_honour_a_machine_choice(settings, store, make_epub):
    c, _ = make_client(settings, store)
    assert convert(c, upload(c, make_epub())).status_code == 201
    assert convert(c, upload(c, make_epub("again.epub")), workers=["local"]).status_code == 404


def test_jobs_expose_the_per_machine_progress_list(settings, store, make_epub):
    c, _ = make_client(settings, store)
    r = convert(c, upload(c, make_epub()))
    assert r.json()["workers"] == [] and "clip_index" not in r.json()


def test_the_cover_is_available_before_converting(settings, store, make_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, make_epub("with.epub", cover=PNG))
    assert c.get(f"/api/uploads/{uid}").json()["has_cover"] is True
    r = c.get(f"/api/uploads/{uid}/cover")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG


def test_no_cover_means_a_404_not_a_made_up_picture(settings, store, make_epub):
    c, _ = make_client(settings, store)
    uid = upload(c, make_epub("plain.epub"))
    assert c.get(f"/api/uploads/{uid}").json()["has_cover"] is False
    assert c.get(f"/api/uploads/{uid}/cover").status_code == 404
    assert c.get("/api/uploads/nope/cover").status_code == 404


def test_the_library_shows_the_cover_the_book_came_with(settings, store, make_epub):
    from app.jobs import Runner
    from conftest import FakeEngine

    c, _ = make_client(settings, store)
    uid = upload(c, make_epub("lib.epub", cover=PNG))
    job = convert(c, uid).json()
    Runner(settings, store, lambda _v: FakeEngine()).run_one(store.claim_next())
    (book,) = c.get("/api/library").json()
    assert book["has_cover"] is True
    r = c.get(f"/api/library/{job['book_md5']}/cover")
    assert r.status_code == 200 and r.content == PNG
