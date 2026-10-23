from __future__ import annotations

import hashlib
import shutil
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field

from . import books, catalog, ingest, m4b, tts
from .books.epub_cover import sniff as sniff_image
from .calibre import CalibreClient, CalibreConfig, CalibreError, CalibreStore, normalize_url as calibre_url
from .books import MIN_CHAPTER_CHARS, ParamError, default_selection
from .config import Settings
from .engine_config import ConfigError, EngineController
from .jobs import JobStore
from .library import Library, LibraryError, md5_file
from .model import ACTIVE, Cover, Plan, Voice
from .remote import WorkerError
from .samples import SampleError, SampleStore
from .uploads import UploadStore
from .watcher import book_language
from .sources import adopt

STATIC = Path(__file__).parent / "static"
TRUTHY = {"1", "true", "yes", "on"}
CHARS_PER_SECOND = 14.5
SNIPPET = 140


class PreviewRequest(BaseModel):
    processor: str = "auto"
    params: dict[str, str] = Field(default_factory=dict)


class WorkerEntry(BaseModel):
    id: str | None = None
    name: str = ""
    url: str
    token: str | None = None
    clear_token: bool = False
    enabled: bool = True


class EngineChoice(BaseModel):
    local_enabled: bool = True
    workers: list[WorkerEntry] = Field(default_factory=list)


class WorkerTest(BaseModel):
    worker_url: str
    worker_token: str | None = None
    worker_id: str | None = None


class CalibreSettings(BaseModel):
    url: str = ""
    username: str = ""
    password: str | None = None
    clear_password: bool = False


class CalibreImport(BaseModel):
    path: str
    title: str = "book"
    format: str
    cover: str | None = None  # Calibre-Web's own cover for the book; used instead of the one inside the file


class BookDetails(BaseModel):
    title: str
    author: str = ""


class ConvertRequest(PreviewRequest):
    selection: list[int] | None = None
    overwrite: bool = False
    title: str | None = None
    author: str | None = None
    model: str | None = None
    voice: str | None = None
    dialog_voice: str | None = None
    temperature: float | None = None
    read_titles: bool | None = None
    workers: list[str] | None = None


def processor_info(proc) -> dict:
    return {"id": proc.id, "label": proc.label, "description": proc.description,
            "params": [{"name": p.name, "label": p.label, "kind": p.kind, "default": p.default,
                        "choices": list(p.choices), "help": p.help} for p in proc.params]}


def snippets(text: str) -> tuple[str, str]:
    flat = " ".join(text.split())
    return flat[:SNIPPET], (flat[-SNIPPET:] if len(flat) > SNIPPET * 2 else "")


def entry_info(entry) -> dict:
    d = asdict(entry)
    d["voice_label"] = entry.voice_label()
    return d


def create_app(settings: Settings, store: JobStore, pool, uploads: UploadStore | None = None,
               samples: SampleStore | None = None, library: Library | None = None,
               engine: EngineController | None = None, calibre_http=None) -> FastAPI:
    app = FastAPI(title="ebook to audiobook")

    @app.exception_handler(WorkerError)
    async def worker_failed(_request, exc: WorkerError):
        return JSONResponse({"detail": str(exc)}, status_code=502)
    calibre_store = CalibreStore(settings.calibre_config)

    @app.exception_handler(CalibreError)
    async def calibre_failed(_request, exc: CalibreError):
        return JSONResponse({"detail": str(exc)}, status_code=502)
    uploads = uploads or UploadStore(settings.uploads)
    samples = samples or SampleStore(settings.samples)
    library = library or Library(settings.library)
    uploads.sweep()

    def pool_now():
        """The machine that answers for voice lists, samples and cloning."""
        return engine.pool if engine is not None else pool
    md5s: dict[str, str] = {}

    def md5_of(up) -> str:
        if up.id not in md5s:
            md5s[up.id] = md5_file(up.path)
        return md5s[up.id]

    def book_dir_or_404(md5: str):
        try:
            if library.manifest(md5) is None:
                raise HTTPException(404, "that book is not in the library")
        except LibraryError as exc:
            raise HTTPException(404, str(exc))

    def spec_from(req_model, req_voice, req_dialog, req_temp, req_titles, language=None) -> Voice:
        d = settings.defaults_for(language)
        own = (req_model or d["model"]) == d["model"]  # default voices belong to the default model only
        spec = Voice(req_model or d["model"], req_voice or (d["voice"] if own else None),
                     settings.temperature if req_temp is None else req_temp,
                     settings.read_titles if req_titles is None else req_titles,
                     req_dialog or (d["dialog_voice"] if own else None))
        if spec.model not in tts.MODEL_MODES:
            raise HTTPException(400, f"unknown model {spec.model!r}")
        return spec

    def loading(exc: tts.ModelLoading) -> HTTPException:
        return HTTPException(503, {"loading": exc.error is None, "message": str(exc)})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/defaults")
    def defaults():
        english = settings.defaults_for("en")
        return {"model": settings.model, "voice": settings.voice, "dialog_voice": settings.dialog_voice,
                "english": english if english["model"] != settings.model else None,
                "temperature": settings.temperature, "read_titles": settings.read_titles,
                "formats": sorted(ingest.SUPPORTED), "min_chapter_chars": MIN_CHAPTER_CHARS,
                "chars_per_second": CHARS_PER_SECOND}

    def engine_or_404() -> EngineController:
        if engine is None:
            raise HTTPException(404, "the voice engine cannot be changed here")
        return engine

    @app.get("/api/engine")
    def engine_status():
        return engine_or_404().view()

    @app.put("/api/engine")
    def engine_save(choice: EngineChoice):
        try:
            return engine_or_404().apply(choice.local_enabled, [w.model_dump() for w in choice.workers])
        except ConfigError as exc:
            raise HTTPException(422, str(exc))

    @app.post("/api/engine/test")
    def engine_test(request: WorkerTest):
        try:
            return engine_or_404().test(request.worker_url, request.worker_token, request.worker_id)
        except ConfigError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/workers")
    def workers_status():
        """Every machine that can make speech, with whether it answers now and which models it has loaded."""
        return engine_or_404().status()

    # --- Calibre-Web: browse the catalogue, bring a book into the Convert flow ---------------------

    def calibre_client() -> CalibreClient:
        config = calibre_store.load()
        if not config.configured:
            raise HTTPException(409, "connect Calibre-Web in Settings first")
        return CalibreClient(config, calibre_http)

    def valid_calibre_url(raw: str) -> str:
        try:
            return calibre_url(raw)
        except CalibreError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/calibre")
    def calibre_status():
        c = calibre_store.load()
        return {"configured": c.configured, "url": c.url, "username": c.username, "has_password": bool(c.password)}

    @app.put("/api/calibre")
    def calibre_save(new: CalibreSettings):
        old = calibre_store.load()
        if not new.url.strip():
            calibre_store.save(CalibreConfig())
            return calibre_status()
        url = valid_calibre_url(new.url)
        password = new.password or (None if new.clear_password or old.url != url or old.username != new.username
                                    else old.password)
        calibre_store.save(CalibreConfig(url, new.username.strip() or None, password))
        return calibre_status()

    @app.post("/api/calibre/test")
    def calibre_test(new: CalibreSettings):
        old = calibre_store.load()
        url = valid_calibre_url(new.url)
        password = new.password or (old.password if old.url == url and old.username == new.username else None)
        return CalibreClient(CalibreConfig(url, new.username.strip() or None, password), calibre_http).test()

    @app.get("/api/calibre/feed")
    def calibre_feed(path: str | None = None, q: str | None = None):
        return calibre_client().feed(path, q)

    @app.get("/api/calibre/image")
    def calibre_image(path: str):
        data, kind = calibre_client().image(path)
        return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/calibre/import", status_code=201)
    def calibre_import(request: CalibreImport):
        """Download a book from Calibre-Web into an upload, ready for step 2 of Convert."""
        suffix = "." + request.format.lower().lstrip(".")
        if suffix not in ingest.SUPPORTED:
            raise HTTPException(415, f"unsupported format {request.format!r}")
        client = calibre_client()
        up = uploads.create(request.title + suffix)
        try:
            md5s[up.id] = client.download(request.path, up.path)
        except Exception:
            uploads.delete(up.id)
            raise
        cover = None
        if request.cover:  # Calibre-Web shows the cover from its own library, which can differ from the one in the file
            try:
                data, _kind = client.image(request.cover)
                if (ext := sniff_image(data)):
                    cover = Cover(data, ext)
                    (up.path.parent / ".cover").mkdir(exist_ok=True)
                    (up.path.parent / ".cover" / f"cover.{ext}").write_bytes(data)
            except CalibreError:
                pass  # keep the cover inside the file
        updated = False
        if cover is not None and library.manifest(md5s[up.id]) is not None:
            folder = library.dir(md5s[up.id])
            for entry in library.set_cover(md5s[up.id], cover) or []:
                m4b.replace_cover(folder / entry.file, cover)
            updated = True
        return {"id": up.id, "filename": up.name, "md5": md5s[up.id],
                "cover": "calibre-web" if cover else "file", "library_cover_updated": updated}

    @app.get("/api/models")
    def model_catalog():
        """The models this server knows how to use, with a rough download size."""
        return [catalog.describe(m) for m in catalog.CATALOG]

    @app.post("/api/workers/{node_id}/models/{model}", status_code=202)
    def fetch_model(node_id: str, model: str):
        """Have a machine download and load a model now, so a later conversion finds it ready."""
        try:
            return engine_or_404().fetch_model(node_id, model)
        except ConfigError as exc:
            raise HTTPException(404, str(exc))

    @app.get("/api/processors")
    def processors():
        return [processor_info(p) for p in books.PROCESSORS.values()]

    @app.get("/api/voices")
    def voices(model: str | None = None):
        try:
            result = pool_now().voices(model or settings.model)
            return {"voices": result.voices, "ready": result.ready, "error": result.error}
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except tts.ModelLoading as exc:
            raise loading(exc)

    @app.get("/api/voice-sample")
    def voice_sample(model: str | None = None, voice: str | None = None, text: str | None = None,
                     temperature: float | None = None):
        spec = spec_from(model, voice, None, temperature, None)
        try:
            current = pool_now()
            current.ensure(spec.model)
            path = samples.get(current, spec.model, spec.voice, text, spec.temperature)
        except tts.ModelLoading as exc:
            raise loading(exc)
        except SampleError as exc:
            raise HTTPException(400, str(exc))
        return FileResponse(path, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.post("/api/voices", status_code=201)
    def clone_voice(name: str = Form(...), file: UploadFile = File(...)):
        with tempfile.TemporaryDirectory() as d:
            upload = Path(d) / "upload"
            with upload.open("wb") as out:
                shutil.copyfileobj(file.file, out)
            try:
                pool_now().add_clone(name, upload, settings.model)
            except tts.CloneError as exc:
                raise HTTPException(400, str(exc))
            except tts.ModelLoading as exc:
                raise loading(exc)
        return {"id": name.strip(), "cloned": True}

    @app.delete("/api/voices/{name}", status_code=204)
    def delete_voice(name: str):
        if not (engine.remove_clone(name) if engine is not None else pool.remove_clone(name)):
            raise HTTPException(404, "no such cloned voice")

    @app.post("/api/uploads", status_code=201)
    def create_upload(file: UploadFile = File(...)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in ingest.SUPPORTED:
            raise HTTPException(415, f"unsupported format {suffix or file.filename!r}")
        up = uploads.create(file.filename or "book" + suffix)
        digest = hashlib.md5()
        with up.path.open("wb") as out:
            while chunk := file.file.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
        md5s[up.id] = digest.hexdigest()
        return {"id": up.id, "filename": up.name, "md5": md5s[up.id]}

    def load(upload_id: str, request: PreviewRequest):
        up = uploads.get(upload_id)
        if up is None:
            raise HTTPException(404, "that upload has expired; upload the book again")
        try:
            return up, books.parse(up.path, request.processor, request.params, cache_dir=up.cache_dir)
        except (ParamError, ingest.IngestError, ingest.UnsupportedFormat) as exc:
            raise HTTPException(422, str(exc))

    @app.get("/api/uploads/{upload_id}")
    def upload_info(upload_id: str):
        up = uploads.get(upload_id)
        if up is None:
            raise HTTPException(404, "that upload has expired; upload the book again")
        try:
            source = books.load_source(up.path, up.cache_dir)
        except (ingest.IngestError, ingest.UnsupportedFormat) as exc:
            raise HTTPException(422, str(exc))
        md5 = md5_of(up)
        known = library.manifest(md5) or {}
        return {"id": up.id, "filename": up.name, "md5": md5, "kind": source.kind,
                "title": known.get("title") or source.title, "author": known.get("author") or source.author,
                "language": source.language, "has_cover": source.cover is not None,
                "in_library": bool(known), "audio_count": len(library.entries(md5)),
                "processors": [processor_info(p) for p in books.applicable(source)]}

    @app.get("/api/uploads/{upload_id}/cover")
    def upload_cover(upload_id: str):
        up = uploads.get(upload_id)
        if up is None:
            raise HTTPException(404, "that upload has expired; upload the book again")
        try:
            cover = books.load_source(up.path, up.cache_dir).cover
        except (ingest.IngestError, ingest.UnsupportedFormat) as exc:
            raise HTTPException(422, str(exc))
        if cover is None:
            raise HTTPException(404, "this book has no cover")
        return Response(cover.data, media_type="image/png" if cover.ext == "png" else "image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})

    @app.post("/api/uploads/{upload_id}/preview")
    def preview(upload_id: str, request: PreviewRequest):
        up, parsed = load(upload_id, request)
        md5 = md5_of(up)
        defaults_on = set(default_selection(parsed.sections))
        existing = library.audio_by_hash(md5)
        chapters = []
        for i, s in enumerate(parsed.sections):
            head, tail = snippets(s.text)
            digest = books.text_hash(s.title, s.text)
            chapters.append({"index": i, "title": s.title, "chars": s.chars,
                             "seconds": round(s.chars / CHARS_PER_SECOND), "head": head, "tail": tail,
                             "selected": i in defaults_on, "text_hash": digest,
                             "audio": [entry_info(a) for a in existing.get(digest, [])]})
        return {"md5": md5, "title": parsed.title, "author": parsed.author, "language": parsed.language,
                "processor": parsed.processor, "params": parsed.params, "note": parsed.note,
                "audio_count": len(library.entries(md5)), "chapters": chapters}

    @app.post("/api/uploads/{upload_id}/convert", status_code=201)
    def convert(upload_id: str, request: ConvertRequest):
        up, parsed = load(upload_id, request)
        selection = request.selection if request.selection is not None else default_selection(parsed.sections)
        selection = sorted({i for i in selection if 0 <= i < len(parsed.sections)})
        if not selection:
            raise HTTPException(422, "select at least one chapter to convert")
        spec = spec_from(request.model, request.voice, request.dialog_voice, request.temperature, request.read_titles,
                         parsed.language)
        md5 = md5_of(up)
        sig = spec.signature()[:6]
        have = {i for i in selection
                if library.find(md5, books.text_hash(parsed.sections[i].title, parsed.sections[i].text), sig)}
        workers = None
        if request.workers is not None:
            if engine is None:
                raise HTTPException(404, "the voice machines cannot be chosen here")
            workers = tuple(n.id for n in engine.lanes(request.workers))
            if not workers:
                raise HTTPException(422, "choose at least one machine to run the conversion on")
        md5, target = adopt(settings.books, up.path, md5)
        plan = Plan(request.processor, tuple(sorted(parsed.params.items())), tuple(selection),
                    (request.title or "").strip() or None, (request.author or "").strip() or None, request.overwrite,
                    workers)
        job = store.create(target, spec, plan, md5, Path(up.name).stem)
        uploads.delete(upload_id)
        md5s.pop(upload_id, None)
        return {**job.public(), "already_had_audio": len(have)}

    @app.delete("/api/uploads/{upload_id}", status_code=204)
    def delete_upload(upload_id: str):
        uploads.delete(upload_id)

    @app.get("/api/jobs")
    def jobs():
        return [j.public() for j in store.list()]

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        found = store.get(job_id)
        if found is None:
            raise HTTPException(404, "no such job")
        return found.public()

    @app.post("/api/jobs", status_code=201)
    def create_job(
        file: UploadFile = File(...),
        model: str | None = Form(None),
        voice: str | None = Form(None),
        dialog_voice: str | None = Form(None),
        temperature: float | None = Form(None),
        read_titles: str | None = Form(None),
    ):
        """Quick path for scripts: automatic chapter detection, every readable chapter."""
        name = Path(file.filename or "").name
        if Path(name).suffix.lower() not in ingest.SUPPORTED:
            raise HTTPException(415, f"unsupported format {Path(name).suffix or name!r}")
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d) / name
            with tmp.open("wb") as out:
                shutil.copyfileobj(file.file, out)
            md5, target = adopt(settings.books, tmp)
        spec = spec_from(model, voice, dialog_voice, temperature,
                         None if read_titles is None else read_titles.lower() in TRUTHY, book_language(target))
        return store.create(target, spec, md5=md5, title=Path(name).stem).public()

    @app.get("/api/library")
    def library_list():
        return library.list_books()

    @app.get("/api/library/{md5}")
    def library_book(md5: str):
        book_dir_or_404(md5)
        groups: dict[str, list] = {}
        for entry in library.entries(md5):
            groups.setdefault(entry.voice_sig, []).append(entry)
        return {**library.manifest(md5), "groups": [
            {"voice_sig": sig, "label": items[0].voice_label(), "model": items[0].model, "voice": items[0].voice,
             "dialog_voice": items[0].dialog_voice, "temperature": items[0].temperature,
             "seconds": round(sum(a.seconds for a in items)), "bytes": sum(a.bytes for a in items),
             "files": [entry_info(a) for a in items]} for sig, items in groups.items()]}

    @app.patch("/api/library/{md5}")
    def library_rename(md5: str, details: BookDetails):
        """Change a book's title and author in the library, and in the tags of every chapter file."""
        book_dir_or_404(md5)
        title, author = details.title.strip(), details.author.strip() or "Unknown"
        if not title:
            raise HTTPException(422, "the title cannot be empty")
        folder = library.dir(md5)
        for entry in library.set_details(md5, title=title, author=author):
            m4b.retag(folder / entry.file, {"album": title, "artist": author, "album_artist": author})
        return library.manifest(md5)

    @app.get("/api/library/{md5}/cover")
    def library_cover(md5: str):
        book_dir_or_404(md5)
        name = (library.manifest(md5) or {}).get("cover")
        if not name:
            raise HTTPException(404, "this book has no cover")
        return FileResponse(library.dir(md5) / name)

    @app.get("/api/library/{md5}/audio/{file}")
    def library_audio(md5: str, file: str, download: bool = False):
        book_dir_or_404(md5)
        entry = next((a for a in library.entries(md5) if a.file == file), None)
        if entry is None:
            raise HTTPException(404, "no such audio file")
        return FileResponse(library.dir(md5) / entry.file, media_type="audio/mp4", filename=entry.file,
                            content_disposition_type="attachment" if download else "inline")

    @app.delete("/api/library/{md5}/audio/{file}", status_code=204)
    def library_delete_audio(md5: str, file: str):
        book_dir_or_404(md5)
        if not library.remove_audio(md5, file):
            raise HTTPException(404, "no such audio file")

    @app.delete("/api/library/{md5}", status_code=204)
    def library_delete_book(md5: str):
        book_dir_or_404(md5)
        library.delete_book(md5)

    @app.get("/api/library/{md5}/book.m4b")
    def library_m4b(md5: str, group: str, download: bool = True):
        """One chaptered M4B built from a voice's chapter files, joined without re-encoding."""
        book_dir_or_404(md5)
        entries = sorted((a for a in library.entries(md5) if a.voice_sig == group), key=lambda a: a.index)
        if not entries:
            raise HTTPException(404, "no audio for that voice")
        folder = library.dir(md5)
        digest = hashlib.sha1("|".join(f"{a.file}:{(folder / a.file).stat().st_size}" for a in entries).encode()).hexdigest()[:12]
        out = settings.m4b_cache / f"{md5}-{group}-{digest}.m4b"
        info = library.manifest(md5) or {}
        if not out.exists():
            cover_name = info.get("cover")
            cover = None
            if cover_name:
                cover = Cover((folder / cover_name).read_bytes(), Path(cover_name).suffix.lstrip("."))
            refs = [m4b.ChapterRef(a.title, folder / a.file, m4b.probe_seconds(folder / a.file)) for a in entries]
            m4b.concat_m4b(refs, out, info.get("title", ""), info.get("author", ""), info.get("language", ""), cover)
        name = m4b.safe_name(info.get("title", "audiobook"), "audiobook") + ".m4b"
        return FileResponse(out, media_type="audio/mp4", filename=name,
                            content_disposition_type="attachment" if download else "inline")

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        found = store.request_cancel(job_id)
        if found is None:
            raise HTTPException(404, "no such job")
        return found.public()

    @app.post("/api/jobs/{job_id}/retry")
    def retry(job_id: str):
        found = store.retry(job_id)
        if found is None:
            raise HTTPException(404, "no such job")
        return found.public()

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete(job_id: str):
        found = store.get(job_id)
        if found is None:
            raise HTTPException(404, "no such job")
        if found.state in ACTIVE:
            raise HTTPException(409, "cancel the job before deleting it")
        store.delete(job_id)

    return app
