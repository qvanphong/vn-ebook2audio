# Architecture

## The pieces and how they connect

```
                     browser (one HTML file, polls the API)
                                  │ HTTP
            ┌─────────────────────▼────────────────────────┐
            │  server  (FastAPI, app/api.py)                │
            │  uploads · preview · library · machines ·     │
            │  voices · Calibre-Web · jobs                  │
            └───┬───────────────┬───────────────┬───────────┘
                │ queue          │ files          │ machines
        ┌───────▼──────┐  ┌──────▼───────┐  ┌────▼──────────────────────────┐
        │ JobStore     │  │ Library      │  │ EngineController (the list)    │
        │ (SQLite)     │  │ data/library │  │  Node "This server" ─ ModelPool ─ LocalBackend ─ models
        └───────┬──────┘  └──────▲───────┘  │  Node "Laptop"      ─ ModelPool ─ RemoteBackend ─┐
                │ claim          │ add chapter└────────────────────────────────────────────────┼─┘
        ┌───────▼──────────────┐ │                                                              │ HTTP
        │ Runner thread         │ │                                                       ┌─────▼──────┐
        │  process(job)         │─┘                                                       │ voice      │
        │   Conversion          │  one lane thread per machine, chapters taken from a    │ worker     │
        │    lane · lane · …    │  shared queue                                          │ (worker.py)│
        └──────────────────────┘                                                         └────────────┘
```

## Modules (`nas-audiobook/app/`)

| Module | Responsibility |
|---|---|
| `__main__.py` | Starts everything: settings, job store, machine list, optional preload, runner thread, watcher thread, the web server |
| `api.py` | All HTTP routes of the server, request/response models, error mapping |
| `config.py` | `Settings` from environment variables; paths inside `DATA_DIR`; per-language defaults |
| `model.py` | Plain data types: `Clip`, `Chapter`, `Book`, `Voice` (voice settings and their signature), `Plan` (how a job splits and where it runs), `State` |
| `books/` | Loading books and splitting them into chapters: `source.py` (formats, covers, language), `epub_cover.py`, `epub_text.py`, `epub_processors.py`, `text_processors.py`, `base.py` (types, hashing, default selection), `__init__.py` (registry and the automatic processor) |
| `ingest.py` | Builds a `Book` (spoken chapters) from a parse and a plan |
| `text.py` | Cleaning, sentence splitting, dialogue, pauses |
| `jobs.py` | `JobStore` (SQLite), `Runner`, `process()`, `Conversion` (lanes), `Progress` |
| `tts.py` | `ModelPool`, backends, handles (`LocalHandle`, `SupertonicHandle`), clone store, `prepare()` |
| `catalog.py` | The model catalogue, "is it downloaded", download progress, cache folders |
| `engine_config.py` | `EngineController`: the machine list, its saved form, live status |
| `fleet.py` | `Node`, `WorkerConfig`, and the `probe()` of a worker |
| `remote.py` | `RemoteBackend`: talks to a worker (retries, ensure-model, phoneme path) |
| `worker.py` | The voice worker's own HTTP app |
| `frontend.py` | The text frontend (chunks, phonemes, joining) for phoneme-only workers |
| `library.py` | Library folders and manifests |
| `m4b.py` | ffmpeg: encode a chapter, retag, join an M4B |
| `sources.py`, `uploads.py` | Original files by MD5; uploads waiting for a decision |
| `samples.py` | Voice sample MP3s |
| `calibre.py` | The Calibre-Web (OPDS) client and its saved settings |
| `watcher.py` | The drop folder |
| `bench.py` | Speed benchmark |
| `static/index.html` | The page |

## Startup

1. `catalog.configure_cache_env()` keeps the English model's folder inside `HF_HOME`.
2. Settings are read; `DATA_DIR` subfolders are created.
3. The job store opens `jobs.sqlite` (adding columns an older database lacks).
4. The machine list is loaded from `config/engine.json` (or built from `TTS_BACKEND`). Building it creates a `ModelPool`
   per machine but loads nothing.
5. If `PRELOAD_MODELS` names models, a background thread loads them. By default none.
6. The runner thread starts; its first act is to put any job left `running` by a crash or restart back to `queued`.
7. The watcher thread starts; the web server starts.

So the service can start with no model on disk, and even with no speech packages installed.

## A job from start to finish

`process(job)` in `jobs.py`:

1. Parse the book with the job's processor and settings, apply the chapter selection, build the spoken chapters
   (`ingest.build_book`).
2. Register the book in the library (`ensure_book`: writes `book.json`, saves or replaces the cover).
3. Decide per chapter: **reuse** if a file for the same chapter text and voice signature already exists (and the job
   does not replace), or if a retried job already finished it; otherwise it is **pending**. If nothing is pending, no
   machine is touched.
4. Ask the machine list for the lanes (`plan.workers`, or every machine that is on). None left is an error.
5. `Conversion.run()`: start one thread per lane and wait.

### A lane

```
prepare:  pool.prepare(voice settings)   download/load the model (ensure_model on a worker), check or upload voices
loop:     take the next pending chapter     (longest first when there are several lanes)
          for each clip: check for cancel, synthesize, add the pause
          encode to .m4a, add to the library, record progress
```

- Chapters are taken from a shared queue under a condition variable, so a free lane always takes the next one.
- A lane that raises a machine-level error (`LaneError`: offline, voice missing, model not loadable) hands its chapter
  back and drops out. Any other exception is a real failure and stops the job.
- A lane still preparing when the book is finished is abandoned (its thread is a daemon; the model it was loading keeps
  loading and is useful next time).
- On cancel, each lane stops at the next sentence; the chapter in progress is discarded.
- If the lanes all end with chapters left, the job fails with `NoWorkers` and each machine's reason.

### Progress

A `Progress` object holds the per-chapter list and one record per lane, and writes both to the job row (`chapters`,
`workers` JSON columns, `chapters_done`) on every change. The page polls the job list every three seconds and renders
the lane lines from it.

### Resume

State that survives a crash is on disk: finished chapters are real files with manifest entries, and the job row keeps
the per-chapter list. On restart a `running` job becomes `queued`; when it runs, finished chapters are reused. A job
told to replace existing audio remembers which chapters it has already redone, so a retry does not redo them.

## The model pool

`ModelPool` (one per machine) owns the loaded models of that machine.

- `get(model)` loads through the backend once; later calls return the same handle. A per-model load lock means a slow
  load never blocks the short state lock, so the voice list stays responsive.
- `start_loading(model)` loads in a background thread; `status()` reports each model as `ready`, `loading`,
  `downloading`, `downloaded`, `absent` or `error`, with the bytes arrived during a download.
- A per-model inference lock makes concurrent calls take turns (the voice samples and a job share the model).
- `prepare(voice settings)` is what a lane calls before its first chapter: ask the backend to make the model available
  (a worker downloads it), load it, verify the narrator and dialogue voices exist, and upload any cloned voice the
  machine lacks.
- Cloned voices are enrolled into each loaded handle that can clone, from the shared clone store. The English model
  cannot clone, so cloned voices are hidden for it even before it loads.
- A *handle* is what a loaded model looks like: `list_preset_voices`, `voice_ids`, `infer`, `add_voice`,
  `remove_voice`, and `can_clone`. `LocalHandle` wraps VieNeu, `SupertonicHandle` wraps Supertonic, `RemoteHandle`
  stands in for a model living on a worker.

## The machine list

`EngineController` builds a `Node` per machine, each with its own pool. Editing the list reuses the pool of a worker
whose address and token did not change (so a loaded model stays loaded) and starts a new one otherwise. A running job
holds its own references to its nodes, so editing the list never disturbs it. `status()` probes workers in parallel and
keeps each answer for four seconds.

## Concurrency summary

| What | How it is protected |
|---|---|
| Job table | One lock around every SQLite call (`check_same_thread=False`) |
| Library manifests | An RLock; manifests are written to a temporary file and moved into place |
| A model's state | The pool's RLock; a separate lock per model for loading and for inference |
| Lane scheduling | A condition variable around the pending queue and the in-flight count |
| Progress | A lock; each update rewrites the job's JSON columns |
| Text frontend (phoneme workers) | One shared instance with its own lock |

Jobs run one at a time because a model already uses all the threads it is given; chapters within a job run in parallel
only across *machines*.

## The page

`app/static/index.html` is a single file with inline CSS and JavaScript: no build step, no framework. It polls: jobs
every 3 seconds, machine status every 4 seconds on Settings and 6 seconds on Convert (while a book is open), voices
every 3 to 15 seconds while a model loads. The Library re-renders only cards whose data changed, so a playing track is
never interrupted. Because the file is read from disk on each request, a changed page appears on reload, but changed
API code needs a server restart.

## Encoding

Audio from a model is float samples. `m4b.encode_chapter` pipes them to `ffmpeg` (raw float in, AAC 64 kbit/s mono out
in an MP4 container with `+faststart`), with tags and the cover as an attached picture, writing to a temporary name and
renaming on success. `concat_m4b` joins chapter files with ffmpeg's concat demuxer without re-encoding and writes
chapter marks from an `ffmetadata` file. `retag` copies streams with new tags.
