# Getting started

## What you need

| Need | Why |
|---|---|
| Python 3.12 (native install) or Docker | The service is a Python app |
| `ffmpeg` and `ffprobe` | Encoding chapters to AAC, tagging, joining an M4B |
| `poppler-utils` (`pdftotext`, `pdftoppm`) | PDF text and PDF covers. Optional unless you convert PDFs |
| Calibre's `ebook-convert` | Only for MOBI, AZW/AZW3, FB2, DOCX, RTF, ODT, LIT, HTMLZ and KFX. Optional |
| About 1 to 2 GB of free disk for models | Models download the first time each one is used (details in [Models and voices](voices-and-models.md)) |
| A few hundred MB to 2 GB of memory per loaded model | Nano about 700 MB, Turbo about 1.6 GB on the machine measured |

EPUB, plain text and Markdown need nothing beyond Python and ffmpeg.

## Option A: Docker

```bash
cd nas-audiobook
docker compose up -d --build
```

`docker-compose.yml` starts two services: `ebook2audio` (the server, built from `Dockerfile.server`, port **8765**) and
`audiobookshelf` (port **13378**), with `./data` mounted into the first and `./data/library` mounted into the second as
`/audiobooks`. Delete the `audiobookshelf` block if you already run it and mount `./data/library` into your own
instance instead.

There are two Dockerfiles, one per kind of container:

| Image | Built from | What is in it | Size (arm64, as built) |
|---|---|---|---|
| Server | `Dockerfile.server` | The page, library, queue, the speech models' code, ffmpeg, poppler and Calibre | 2.4 GB |
| Server, no Calibre | `--build-arg WITH_CALIBRE=0` | The same without `ebook-convert` (no MOBI, AZW3, FB2, DOCX, RTF) | not built on its own; an earlier build of this kind was 1.6 GB |
| Server, no models | `--build-arg WITH_ENGINE=0` | For a server that sends all speech to workers | 0.7 GB (with `WITH_CALIBRE=0`) |
| Worker | `Dockerfile.worker` | The voice worker: the speech models' code and ffmpeg, no page or library | 1.5 GB |

Models are not baked into any image: they download into `/data/cache/hf` the first time they are needed, so keep `/data`
on a volume. To build the images by hand:

```bash
docker build -f Dockerfile.server -t ebook2audio-server .
docker build -f Dockerfile.worker -t ebook2audio-worker .
```

A **worker** on another machine runs from `docker-compose.worker.yml`:

```bash
WORKER_TOKEN=choose-a-secret docker compose -f docker-compose.worker.yml up -d --build
```

then you add `http://<that machine>:8780` and the token under Settings → Voice machines. See [Voice workers](workers.md).

## Option B: run it directly

```bash
cd nas-audiobook
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-mac-cpu.txt
DATA_DIR=./data python -m app
```

`requirements-mac-cpu.txt` installs the speech models' code (VieNeu, Supertonic, ONNX Runtime) for a CPU. On a machine
with an NVIDIA GPU, use `requirements-nvidia-gpu.txt` in its place (see [Voice workers](workers.md)). Leave it out if this
machine will only send speech to workers. `onnxruntime` is pinned to 1.22.1 because 1.31 refused to load the Turbo
model from the Hugging Face cache.

Open **http://localhost:8765**.

## The first book

1. Open the page and go to **Convert**.
2. Choose a file under *Upload a book*. The page shows the book's title, author, cover and which chapter-splitting
   methods can read it.
3. Check the chapter list in *Split into chapters*. Change the method if the split looks wrong. Untick chapters you do
   not want (front matter and a table of contents are unticked for you).
4. In *Choose a voice*, pick a model and voice. Press **Hear** on a few voices. An English book starts on the English
   model by itself.
5. Press **Convert N chapters**, read the confirmation, and confirm.
6. The page moves to **Jobs**. Chapters become playable as they finish. When the job is done the book is in
   **Library**.

**The service starts with no model.** Nothing is downloaded or loaded when it starts; a model is fetched the first time
something needs it (a voice list, a sample, or a conversion). That first use is slow: the model is downloaded from
Hugging Face (Nano about 280 MB, English about 390 MB, Turbo about 620 MB) and the voice lists stay empty, saying so,
until it has loaded. The page keeps checking by itself. The service even starts on a machine with no speech packages
installed (for example one that will only use workers); a conversion there fails with a message saying what to install.
To load models at start instead, set `PRELOAD_MODELS` (for example `nano,english`).

## Other ways in

- **Drop folder.** Copy a book into `data/inbox` (any subfolder). When it has stopped changing for a few seconds it is
  queued and the original is moved to `data/books/<md5>/`. Model and voice come from the environment settings and the
  book's language.
- **Quick API for scripts.** `curl -F file=@book.epub http://NAS:8765/api/jobs` queues a book with automatic chapter
  detection and every readable chapter. See [HTTP API](api.md).
- **Calibre-Web.** Connect it in Settings, then pick books in the Calibre tab. See [Calibre-Web](calibre-web.md).

## Adding Audiobookshelf

In Audiobookshelf, add a library of type *Audiobooks* whose folder is the mounted `library` directory (in the compose
file, `/audiobooks`). Each `<md5>/` folder holds one book as many `.m4a` files, tagged with title, author, album and
track number, plus `cover.jpg`. Whether Audiobookshelf groups those folders the way you want has not been tested here.
If you prefer a single file per book, download the **chaptered M4B** from the Library tab instead.

## Making it faster

Run a benchmark on the machine that will do the work:

```bash
python -m app.bench --model nano
python -m app.bench --model turbo
python -m app.bench --model english
```

On a machine with a GPU, compare devices (the report says where the model really ran):

```bash
python -m app.bench --model turbo --device cpu
python -m app.bench --model turbo --device cuda
```

It prints load time, peak memory, the speed as RTF (seconds of work per second of audio; below 1.0 is faster than real
time) and how long a 300,000-character novel would take. If this machine is too slow, add a faster computer as a
[voice worker](workers.md).
