# ebook2audio

Turns ebooks into audiobooks with local text-to-speech. Each chapter becomes an `.m4a` file in a folder named by the MD5 of the book, so the same book is always found again, and a library page shows everything made for each book. No cloud, no account. Full documentation is in `../docs/`.

```
 drop a book ──► inbox/ ─┐
 or upload in the web UI ┴─► preview ─► choose chapters ─► confirm ─► job ─► VieNeu TTS ─► ffmpeg
                                                                                          │
 data/library/<md5 of the book>/  0001-<text>-<voice>.m4a, 0002-….m4a, …, cover.jpg, book.json ◄┘
```

## Run it

```bash
docker compose up -d --build
```

| What | Where |
|---|---|
| Converter UI and API | http://NAS:8765 |
| Audiobookshelf | http://NAS:13378 (add a library pointing at `/audiobooks`; it reads each `<md5>/` folder as one multi-file book from the file tags) |
| Drop folder | `./data/inbox` (any subfolder, files are picked up once they stop changing) |
| Output | `./data/library/<md5 of the book file>/*.m4a`, plus `cover.jpg` and `book.json` (see Library layout) |

Remove the `audiobookshelf` service from `docker-compose.yml` if you already run it and mount `./data/library` into it instead.

There are two Dockerfiles: `Dockerfile.server` (the page, library, queue and speech models; `--build-arg WITH_ENGINE=0` for a small image with no models, `WITH_CALIBRE=0` to leave out Calibre) and `Dockerfile.worker` (a voice worker on its own, started with `docker compose -f docker-compose.worker.yml up -d --build` and a `WORKER_TOKEN`). Models are not in the images: they download on demand into the `/data` volume.

The service starts with no model loaded. The first time something needs one (a voice list, a sample, a conversion) it is downloaded from Hugging Face into `./data/cache/hf`, which can take several minutes; the voice lists stay empty and the page says so, and they fill in by themselves. The voice list is saved to `./data/cache/voices.json` once a model has loaded, so on every later start (and when you switch models) the page shows the saved voices at once while the model loads in the background, then refreshes the list when the model is ready. Samples need the loaded model. The default model is loaded at start; set `PRELOAD_MODELS=nano,turbo` to load more, or `none` to load only on demand. If a download fails, the page shows the error and keeps retrying.

## The flow

Open the web page. Nothing is converted until you confirm.

1. **Upload** a book. The page computes its MD5 and tells you if the book is already in your library. The upload is kept for 24 hours and is not queued.
2. **Split into chapters.** Pick a *book processor* from the drop-down. The page lists the chapters it found with their size, an estimated duration, and a **Peek** at the start and end of each one, so you can see whether the boundaries are right. Switch processor, or edit its settings, and the list updates. Untick chapters you do not want. Short sections and contents pages are listed but unticked. You can also correct the title and author. **Chapters that already have audio are marked**: "✓ has audio" when it was made in the voice chosen below, "audio in another voice" otherwise, and the badges update as you change the voice. **Without audio** selects only the chapters still missing.
3. **Choose a voice.** The voice gallery plays a sample of any voice (the standard sentence, or text you type). Set a narrator, optionally a dialogue voice.
4. **Confirm.** Pressing Convert opens a confirmation with the book, chapter count, estimated length, splitting, voice and the folder the audio goes to. If selected chapters already have audio in this voice it says so and offers **Skip the ones with audio** or **Convert all (replace)**. Chapters that only have audio in another voice are not replaced; the new audio is added next to the old.

The **Jobs** tab shows progress. Each chapter can be played as soon as it is done. The **Library** tab lists every book with its cover, author, chapter count, total length, voices and MD5. Each card shows the book's title and author with its MD5 underneath, so you never have to read the hash to find a book. **Rename** changes the title and author in the library and in the tags of every chapter file; the folder keeps its MD5 name. A renamed book keeps its new name when you upload it again. Open a book to see all of its audio grouped by voice, play a voice from start to end (it moves to the next chapter by itself), delete a single chapter file or the whole book, or download one chaptered M4B for a voice (joined from the chapter files without re-encoding, cached).

Scripts can skip the page: `POST /api/jobs` with a file uses the automatic processor and every readable chapter, and skips chapters that already have audio in that voice.

## Library layout

```
data/
  library/<md5>/               one folder per book, named by the MD5 of the book file
    0001-<text>-<voice>.m4a    one AAC file per chapter, tagged title, artist, album, track, genre
    0002-….m4a
    cover.jpg                  if the book has a cover
    book.json                  manifest: title, author, and for each file the chapter text hash, voice, model, length
  books/<md5>/<file name>      the original book file, stored once under its original name
  cache/                       model files, voice list, voice samples, joined M4B downloads
```

A file name is `<chapter number>-<8 characters of the chapter text hash>-<6 characters of the voice settings hash>.m4a`. The hash covers the chapter title and text (the title is read aloud). That is how the page knows a chapter already has audio even if you split the book differently later: chapters only match when their text is identical, and different voice settings never overwrite each other. Audiobookshelf-style tools can play the folder directly; `book.json` is named so it does not clash with Audiobookshelf's own `metadata.json`.

The title shown in the library comes from the book's metadata, or the file name when the metadata has no title (TXT, PDF, and EPUBs without one), or whatever you typed in the Title box in step 2.

Older versions wrote one M4B per book to `library/<Author>/<Title>/`. Those files are no longer managed; move them out of `library/` if Audiobookshelf shows duplicates.

## Book processors

Different sources build EPUBs differently, so there is no single splitter. Each processor is a small class in `app/books/`; the page shows every registered one that can read the uploaded file.

| Processor | How it splits | Use it when |
|---|---|---|
| Automatic | Tries the ones below in order and keeps the first that finds 2+ real chapters | You do not know. This is the default |
| EPUB table of contents | The EPUB's own TOC gives titles and boundaries, including entries that point inside a file | The book is well made |
| One chapter per file | Each content file in reading order, named by its heading or the TOC | Each chapter is its own file |
| Split at headings | A new chapter at every `h1`, `h1-h2` or `h1-h3` tag (setting), across files | Chapters are headings but the TOC is missing or wrong |
| Chapter title lines (pattern) | A new chapter at every text line matching a regular expression (editable) such as `Chương 3` or `Chapter IV` | Titles are plain paragraphs; also TXT, PDF and converted formats |
| Equal-sized parts | About N characters per part at paragraph boundaries (setting) | Nothing else works |

All EPUB processors read text from every block element and line break, not only paragraphs, because many converted books put the chapter in a `<div>` with `<br>`. A title repeated at the top of its own chapter is removed (and up to three running-header lines above it), because the title is spoken once as a heading.

To add one, write a class with `id`, `label`, `description`, `params`, `accepts(source)` and `split(source, params)`, and register it in `app/books/__init__.py`. The page, the API and the tests pick it up from the registry.

## Formats

| Format | How | Needs |
|---|---|---|
| EPUB | read directly | nothing |
| TXT, Markdown | split on "Chương / Chapter / Part" lines, else about 15,000 character chunks | nothing |
| PDF with a text layer | `pdftotext`, then as text | poppler (in the image) |
| MOBI, AZW3, FB2, DOCX, RTF, ODT and others | converted to EPUB with Calibre | build with `WITH_CALIBRE=1` (default), adds roughly a gigabyte |
| Scanned PDF, DRM-protected files | not supported | |

Build without Calibre by setting `WITH_CALIBRE: "0"` in the compose file.

## Settings

Environment variables on the `ebook2audio` service. Uploads can override model, voice, temperature and title reading per book.

| Variable | Default | Meaning |
|---|---|---|
| `TTS_MODEL` | `nano` | The Vietnamese model for Vietnamese books. `nano` is fast (24 kHz). `turbo` sounds better and is about 3x slower (48 kHz) |
| `TTS_ENGLISH_MODEL`, `TTS_ENGLISH_VOICE` | `english`, model default (`F1`) | What an English book starts with (see English books). `TTS_ENGLISH_MODEL=none` keeps `TTS_MODEL` for every book |
| `TTS_BACKEND`, `TTS_WORKER_URL`, `TTS_WORKER_TOKEN` | `local`, none, none | Where speech is made the first time the server starts; the Settings tab overrides them (see Voice engine) |
| `TTS_VOICE` | model default | A preset voice name, for example `Mỹ Duyên`. List them at `/api/voices?model=nano` |
| `TTS_DIALOG_VOICE` | none | A second voice for dialogue. Quoted speech of 3+ words (`“…”`, `«…»`, `"…"`) and lines starting with a dash are read by it. Unset means one voice for everything |
| `TTS_PRECISION` | `fp32` | Turbo only. `int8` is 4x smaller and meant for CPUs with VNNI. It gave no speedup on the Apple CPU I tried |
| `TTS_TEMPERATURE` | `0.8` | Higher is more expressive and less stable |
| `TTS_DEVICE` | `cpu` | `cpu`, `auto`, `cuda`, `directml`, `coreml` or `openvino`: the hardware the models run on (see `../docs/workers.md`, "Using a GPU"). Also `--device` on `python -m app` and `python -m app.worker` |
| `TTS_THREADS` | all CPUs | ONNX threads. Leaving the runtime default was 2x slower in my Docker test |
| `READ_TITLES` | `1` | Speak each chapter title before its text |
| `PRELOAD_MODELS` | `none` | Comma list of models (`nano`, `turbo`, `english`) to load at start. By default the server and the workers load nothing until a model is needed; an unknown name stops the start with a message |
| `SCAN_INTERVAL` / `SETTLE_SECONDS` | `10` / `5` | Inbox polling and how long a file must be unchanged |
| `DATA_DIR`, `PORT` | `/data`, `8765` | |

**Pronunciations.** Put `from=to` lines in `./data/config/pronunciations.txt` (whole word, case-insensitive, longest match first). Example: `iPhone 15=ai phôn mười lăm`.

**Pauses.** Write `[nghỉ 2s]` or `[pause 1.5]` in the text for an explicit pause of 0.1 to 10 seconds.

## API

| Call | Purpose |
|---|---|
| `POST /api/jobs` (multipart `file`, optional `model`, `voice`, `dialog_voice`, `temperature`, `read_titles`) | Queue a book |
| `GET /api/jobs`, `GET /api/jobs/{id}` | State: `queued`, `running`, `assembling`, `done`, `error`, `cancelled`, with per-chapter progress and `workers`: for each machine its state (`waiting`, `preparing`, `rendering`, `idle`, `done`, `failed`), what it is doing, chapter and clip, chapters done and any error |
| `POST /api/jobs/{id}/cancel`, `/retry`; `DELETE /api/jobs/{id}` | Control |
| `GET /api/engine`, `PUT /api/engine` (`{local_enabled, workers: [{id?, name, url, token?, clear_token?, enabled}]}`) | The saved list of machines. Tokens are never returned (`has_token`). 422 for a bad address, a duplicate, or nothing switched on |
| `POST /api/engine/test` (`{worker_url, worker_token?, worker_id?}`) | Is that address a worker, is the token right, which models it has |
| `GET /api/models`, `POST /api/workers/{id}/models/{model}` | The catalogue of models with a rough download size, and a request for one machine to download and load a model now |
| `GET /api/workers` | Every machine with whether it answers now (`online`, `latency_ms`, `error`), the models it has loaded and its capabilities. Answers are reused for 4 seconds |
| `GET /api/calibre`, `PUT /api/calibre` (`{url, username, password?, clear_password?}`; an empty `url` disconnects), `POST /api/calibre/test` | The saved Calibre-Web connection (the password is never returned), and a connection test that reports the catalogue's name and sections |
| `GET /api/calibre/feed?path=&q=`, `GET /api/calibre/image?path=` | One page of the catalogue (books with formats and cover, sections to open, next page) and a cover, both fetched through this server. 409 until Calibre-Web is connected; 502 with the reason if Calibre-Web refuses or is unreachable |
| `POST /api/calibre/import` (`{path, title, format, cover?}`) | Downloads a book into an upload; with `cover` it also fetches Calibre-Web's own cover and uses it instead of the one inside the file (replacing the library cover if the book is already there). Returns `{id, filename, md5, cover, library_cover_updated}` |
| `GET /api/processors` | Every book processor with its settings |
| `POST /api/uploads` (multipart `file`) | Step 1. Returns an upload id and the book's MD5 |
| `GET /api/uploads/{id}` | MD5, title, author, how much audio the library already holds for this book, and the processors that can read the file |
| `POST /api/uploads/{id}/preview` (`{processor, params}`) | Step 2. Chapters with size, estimated seconds, start and end snippets, default selection, and the audio that already exists for each chapter (voice, model, file) |
| `POST /api/uploads/{id}/convert` (`{processor, params, selection, overwrite, title, author, model, voice, dialog_voice, temperature, read_titles, workers}`; `workers` is a list of machine ids from `/api/workers`, or omitted for every machine that is on) | Queues a job for the selected chapters. Without `overwrite`, chapters that already have audio in this voice are skipped. The reply includes `already_had_audio` |
| `GET /api/voice-sample?model=&voice=&text=` | A short MP3 of a voice, cached on disk |
| `GET /api/voices?model=` | `{voices, ready, error}`. Preset and cloned voices (`cloned: true`). `ready: false` means the list came from the saved copy and the model is still loading. 503 only when nothing has been saved yet |
| `GET /api/library` | Every book: title, author, MD5, audio count, total seconds, voices |
| `GET /api/library/{md5}` | One book with its audio grouped by voice settings |
| `GET /api/library/{md5}/audio/{file}` (`?download=true` to save) | A chapter file, with range requests so players can seek. Playable while the job is still running |
| `PATCH /api/library/{md5}` (`{title, author}`) | Rename a book; rewrites the album and artist tags of its chapter files without re-encoding |
| `GET /api/uploads/{id}/cover` | The book's cover before converting (404 if it has none) |
| `GET /api/library/{md5}/cover` | The cover |
| `GET /api/library/{md5}/book.m4b?group=<voice id>` | One chaptered M4B joined from that voice's files |
| `DELETE /api/library/{md5}/audio/{file}`, `DELETE /api/library/{md5}` | Remove one chapter file, or the book's audio (the original book file is kept) |
| `POST /api/voices` (multipart `name`, `file`) | Clone a voice from a 3 to 15 second clip. Any audio format ffmpeg reads. Works in both models |
| `DELETE /api/voices/{name}` | Remove a cloned voice |
| `GET /healthz` | Liveness |

```bash
curl -F file=@book.epub -F model=nano http://NAS:8765/api/jobs
```

There is no login. Keep it on your LAN or behind your reverse proxy's auth.

## How it survives restarts

Each chapter is encoded to `.m4a` and moved into the library the moment it finishes, so nothing is lost but the chapter in progress. If the container stops mid-book, the job goes back to `queued` on start; chapters that already have audio in the same voice are reused, so it carries on where it stopped. A job that was told to replace existing audio remembers which chapters it has already redone, so a retry does not redo them.

Sentence-level audio is not cached, because float audio for a long book would take gigabytes.

## English books

An English book is read by a separate English model, **Supertonic** (Supertone, 44.1 kHz, 10 voices: F1 to F5 and M1 to M5). The page picks it for you: upload an English book and the model switches to **English**, the voices become the English ones, and a note says so; a Vietnamese book switches back to your Vietnamese default. You can change the model by hand after that. Scripts (`POST /api/jobs`) and the inbox folder make the same choice from the book's language, and an explicit `model` always wins.

- **Language** comes from the book's own language tag, or from its text when the tag is missing; a book with neither and no Vietnamese letters counts as English. Books in other languages keep the Vietnamese default for now. Supertonic itself covers about 30 languages, so adding more is a matter of mapping a language to it, which I have not done.
- **Voices.** English and Vietnamese voices are different sets. A Vietnamese default voice (`TTS_VOICE`) never carries over to an English book, and picking a voice the model does not have is caught before the book starts. The narrator and dialogue voices work as before (quoted speech is read by the second voice). **Expressiveness** does not apply and is hidden.
- **No cloning.** The English model has a fixed set of voices. Cloned voices are not offered for it.
- **Text.** The splitter keeps `Dr.`, `Mr.`, `e.g.`, `a.m.`, `U.S.` and similar inside their sentence. Numbers, dates and money are spoken by the model's own text normaliser.
- **Where it runs.** On this server, or on any worker that has it, like the other models: a worker lists it in `/health`. A phone worker that runs only the Nano networks cannot do English and is greyed out in "Run on" for an English book.
- **First use** downloads about 400 MB from Hugging Face (into the same cache as the other models). The weights are under the OpenRAIL-M licence, which has use restrictions; the code is MIT. Supertone's repository is archived, so the version is pinned (`supertonic==1.3.1`) and will not get fixes.

How the choice was made. I rendered the same 10 English sentences with each model and had a speech recogniser (Whisper `small.en`) write down what it heard, then counted word errors on the 7 sentences with no digits (119 words). That measures whether the words are clear, not whether the voice is pleasant, and I have not listened to any of it.

| Model | Word errors | Speed on the M1 Pro (4 threads) |
|---|---|---|
| English (Supertonic), voice F1 | 1.7 % (2 of 119) | 0.23 s of work per second of audio |
| Vietnamese Turbo | 7.6 % (9 of 119) | 0.31 |
| Vietnamese Nano | 20.2 % (24 of 119) | 0.20 |

Nano is poor at English (one sentence came out as "about a letter back" for "a battered leather bag"), and Turbo is usable but misreads small words, so the English model is the default for English. Ten sentences is a small test: use it as a direction, not a precise score.

## Calibre-Web

The **Calibre** tab browses your Calibre-Web library and sends a book straight into Convert: pick a book, press **Convert EPUB** (or another format), and it is downloaded to this server and opens at step 2 as if you had uploaded it. The book keeps its content-based id, so the library recognises it if you have converted it before.

Connect it in **Settings → Calibre-Web** with the address (for example `http://192.168.1.10:8083`), username and password, press **Test**, then **Save**. It uses Calibre-Web's OPDS catalogue, so **Enable OPDS** must be on (Admin → Edit Basic Configuration → Feature Configuration); the test tells you if it is not. The password is stored in `data/config/calibre.json` (mode 600) and is never sent back to the page; a blank password field keeps the saved one, and **Disconnect** deletes it.

- The tab opens on Recently added, with the catalogue's other sections (authors, series and so on, whatever your Calibre-Web offers) one click away from the **Library** link in the trail. Search asks Calibre-Web, so it searches what Calibre-Web searches. **Load more** continues a long list.
- A book lists the formats this app can read, best first (EPUB, AZW3, MOBI, FB2, DOCX, PDF, TXT); a format it cannot read (CBZ, for example) is named but has no button. EPUB splits into chapters best, so it is the first choice.
- The page only ever names a path on the configured server. This server resolves it against that address and refuses anything else, so the page cannot make it fetch from elsewhere. Downloads over 300 MB are refused.
- Plain HTTP on your own network is what most home setups use; the password then travels unencrypted between this server and Calibre-Web, as it does for any app that talks to it that way. Use an `https://` address if you have one.

## Voice machines: this server and any number of workers

Speech is the heavy part. By default this server runs the models itself. In **Settings → Voice machines** you can also add any number of **voice workers**, each any other machine that runs `python -m app.worker`: a fast laptop or desktop, or, if you can run the models there, a phone. The library, the files, the page and the job queue always stay on this server. The list is saved in `data/config/engine.json` (mode 600) and survives restarts; a file saved by an earlier version (one worker) is read as it was.

- **This server** is on by default. It is greyed out if the voice models are not installed here.
- **Add a voice worker** takes a name, an address and, if the worker has one, a token. **Test** checks the address, the token, the protocol and which models the worker has loaded, and fills in the name the worker reports. A blank token field keeps the saved one; "forget the saved token" removes it. **Save** applies the list.
- **Sharing a book.** In Convert, step 3 has a **Run on** list: every machine with a green or red dot (reachable or not) and a note. Tick the ones that should share this book. Chapters go to whichever machine is free, so faster machines simply do more. With several machines, the longest chapters go out first so the last chapter is not a long one on a slow machine; with one machine chapters go in book order. A machine that is off, cannot run the chosen model (a phone with Turbo) or cannot use a cloned voice (a phone) is greyed out with the reason. Your ticks are remembered in the browser.
- **Making each machine ready.** Before a machine takes its first chapter, this server makes it match your choice: the model is downloaded and loaded if it was not (on the worker, this happens the first time anyone asks), the voice must exist, and a cloned voice the machine does not have is uploaded to it. The Jobs tab shows each machine's state ("loading the Nano model (the first time this downloads it)", "sending the cloned voice", then the chapter and clip it is on). A machine that is still loading does not hold anyone up: the others start, and it joins when ready.
- **When a machine fails** (offline, missing voice, cannot load), the job drops it, puts its chapter back, and carries on with the rest; the job line says which machine dropped out and why. If every machine fails the job stops with each reason, finished chapters stay in the library, and Retry continues from the next chapter. A mistake that is not a machine problem (a bug while making audio) stops the whole job.
- **Changing the list is safe at any time.** A conversion keeps the machines it started with. New conversions use the new list. There is no longer a "cannot change while a book is rendering" lock.
- The voice list, samples and cloning come from the first machine that is switched on (this server if it is on, otherwise the first worker). Cloned voices are kept here and sent to each other machine when a conversion needs them.
- If a worker is off, each request to it is retried for about 20 seconds before the machine is dropped. The voice list still shows from a saved copy.

Start a worker on the machine that will do the work (Python 3.12 and ffmpeg installed):

```bash
pip install -r requirements.txt -r requirements-engine.txt
WORKER_NAME="Laptop" WORKER_TOKEN=choose-a-secret python -m app.worker
```

| Worker variable | Default | Meaning |
|---|---|---|
| `WORKER_PORT` | `8780` | Port to listen on |
| `WORKER_NAME` | none | A name the worker reports in `/health`; Test fills it in for you |
| `WORKER_TOKEN` | none | If set, every request needs `Authorization: Bearer <token>`. Without it anyone who can reach the port can use the worker, so set one |
| `DATA_DIR` | `/data` | Where it keeps cloned voices and its voice list |
| `TTS_THREADS`, `TTS_PRECISION` | as for the server | How the models run once loaded |
| `PRELOAD_MODELS` | `none` | A worker is passive: it starts empty and gets a model when the server asks for it. Set a comma list (`nano,english`) only if you want models ready before the first request |
| `HF_HOME` | the Hugging Face default | Where models are downloaded. The English model's folder (`supertonic3`) is kept inside it, so one mounted folder holds every model |

To start this server with one worker and without opening the page, set `TTS_BACKEND=remote`, `TTS_WORKER_URL=http://host:8780` and `TTS_WORKER_TOKEN`. That turns this server off and adds the worker. Once something is saved in the UI it takes priority over these.

**A passive worker.** The worker needs no model configured. This server has the list of models it supports (`GET /api/models`: Vietnamese Nano, Vietnamese Turbo, English), and a conversion tells each machine which model and voice it needs. For each machine, before its first chapter:

1. The server asks the worker for the model (`POST /models/{model}/ensure`). If the worker already has it loaded, it is used at once. If the files are on disk, the worker loads them. If it has never had the model, it **downloads it first**, and the job starts when the download and load are finished. A worker that cannot run the model (the package is not installed, or it is a phone that only runs Nano) says so and drops out; the others carry on.
2. The Jobs tab shows each stage as the worker reports it: "asking the worker for the Vietnamese · Nano model", "downloading … (182 of about 280 MB)", "loading …", then the chapter it is on. The download size is counted from the worker's own cache folder, and the "about" figure in the catalogue is a rough size.
3. The voice named in the request must exist on the worker. A preset it lacks is reported by name; a cloned voice it never had is uploaded to it.

You can also do step 1 ahead of time: in **Settings → Voice machines** each machine lists every model as `ready`, `on disk`, `downloading`, `not downloaded` or `cannot run here`, with a **Download** or **Load** button. "Run on" in Convert says what each machine will have to do for the book you are about to convert ("will download it (about 390 MB) first, then start").

**What the worker speaks (protocol 3; protocols 1 and 2 are still understood).** Anything that implements this can be a worker, including a future Android app. A worker without `/models` (protocol 2) still works: the server waits for it the old way, by asking for its voices, which makes it load the model.

| Call | Meaning |
|---|---|
| `GET /models` | `{"models": {"nano": {"state", "ready", "loading", "downloaded", "supported", "bytes", "size_mb", "error"}, ...}}`. `state` is `ready` (in memory), `loading`, `downloading` (with `bytes` arrived so far), `downloaded` (on disk, not loaded), `absent` or `error`. `supported` is false if this worker could never run it |
| `POST /models/{model}/ensure` | 202 at once; the worker downloads and loads the model in the background and `GET /models` shows how far it got. Asking again changes nothing. 400 for an unknown model, 501 if `supported` is false |
| `GET /health` | `{"ok": true, "protocol": 3, "name": "Laptop", "models": {"nano": {"ready": bool}, ...}, "capabilities": {"synthesize": bool, "infer": ["nano"], "clone": bool}}` |
| `GET /voices?model=nano` | `{"voices": [{"id", "label", "cloned"}], "ready": bool, "error": null}`. 503 `{"detail": {"loading": true}}` while the model loads |
| `POST /synthesize` with `{"model", "text", "voice", "temperature"}` | Text in. Mono 16-bit little-endian PCM, sample rate in `X-Sample-Rate` (24000 for nano, 48000 for turbo). 501 if `capabilities.synthesize` is false |
| `POST /infer` with `{"model", "phonemes", "voice", "steps", "cfg", "speed", "seed"}` | One already-phonemised chunk in, one chunk of audio out (PCM as above, trimmed and faded). Nano only. The worker does no text work |
| `POST /clones` (multipart `name`, `file`), `DELETE /clones/{name}` | Add or remove a cloned voice. 501 if `capabilities.clone` is false |

**A worker that only takes phonemes.** `capabilities.synthesize: false` is how a small worker (a phone) says it runs only the neural networks. This server then does the text work itself: it normalises the text, splits it into chunks of up to 140 characters, turns each into phonemes and pauses, sends the chunks to `/infer`, and joins the audio. That needs VieNeu's text code, which needs no ONNX Runtime and no PyTorch:

```bash
pip install -r requirements.txt
pip install --no-deps vieneu==3.8.3 && pip install sea-g2p
```

On this Mac that gave a 127 MB environment instead of 724 MB, and the server used 115 MB of memory. Voice cloning is switched off while such a worker is active (the Settings test says so, and cloned voices are hidden). To try it without a phone, start the Python worker with `WORKER_ACCEPTS=phonemes`. `golden/` and `tools/make_golden.py` hold reference data for checking a port of the Nano loop, and `../android-spike/` is a test app for a phone.

**Phones.** There is no official VieNeu build for Android, and the worker above is Python. I did port the Nano loop to Kotlin and ran it on a Pixel 8 Pro (`../android-spike/`): the output matches the Python reference exactly, and with the int8 copy of the model it settles at about 0.6 s of work per second of audio (about 1.0 in full precision), because the phone throttles after a few seconds of load. The int8 audio has not been listened to. There is no Android worker app yet; the protocol above is the contract one would need to meet, and this server does not care what is behind it. Details are in `../docs/phone-worker.md`.

## Will my CPU be fast enough?

The models are the same files on every machine; only the speed changes. Measure yours before you queue a long book:

```bash
docker compose run --rm ebook2audio python -m app.bench --model nano
docker compose run --rm ebook2audio python -m app.bench --model turbo
docker compose run --rm ebook2audio python -m app.bench --model turbo --precision int8
```

It prints the model's load time, peak memory, the speed as RTF (seconds of work per second of audio; below 1.0 is faster than real time) and how long a 300,000 and a 1,000,000 character book would take. Use `--threads N` to match your CPU.

Measured on an M1 Pro with 4 threads, running natively: Nano RTF 0.26 and about 700 MB of memory; Turbo RTF 0.33 and about 1.6 GB. The same Nano model inside Docker Desktop on that Mac, also 4 threads, ran at RTF 1.69, about 6 times slower, so the runtime matters as much as the chip. Run the benchmark on the machine that will do the work, not on your laptop.

Conversion is a batch job, so a speed slower than real time is fine for audiobooks you start and leave. It only matters for how long you wait.

## Develop

```bash
python -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest          # uses a fake TTS engine, needs ffmpeg and ffprobe on PATH
DATA_DIR=./data .venv/bin/python -m app
```

Pin `onnxruntime==1.22.1`. Version 1.31.0 refused to load the Turbo model from the Hugging Face cache ("External data path escapes model directory").

## What was tested

Run on an M1 Pro Mac with Docker Desktop (10 CPUs, 8 GB), Nano model, preset voice `Mỹ Duyên`. I have not listened to the audio quality.

Checked on the current per-chapter library version:

| Check | Result |
|---|---|
| Unit and integration tests (fake TTS engine, real ffmpeg, real PDF through `pdftotext`) | 102 pass inside the Docker image; 100 on macOS, where 2 PDF tests skip without poppler |
| Browser, a book uploaded for the first time: pick a voice, select one chapter, Convert | The confirmation modal listed book, chapters, voice and `library/<md5>/` with no warning. The job wrote `0001-…m4a` into a folder named by the book's real MD5 (matches `md5` of the file) |
| Browser, same book uploaded again | A banner said the book is in the library. Chapter 1 showed "✓ has audio"; switching the voice to Default changed it to "audio in another voice". The modal warned that 1 selected chapter already had audio and offered **Skip the 1 with audio** or **Convert all 2 (replace)** |
| Browser, **Skip** | The job converted only chapter 2 into the same folder, which then held 2 files and one `book.json` |
| Browser, Library tab | The book card showed title, author, 2 audio files, 74 s, voice, MD5. Opened, it listed both chapters under "Nano · Mỹ Duyên". Playing chapter 1 (38 s) moved on to chapter 2 by itself. The single-M4B download returned an `audio/mp4` file (0.5 MB) |
| Files on disk | `library/<md5>/0001-…m4a`, `0002-…m4a`, `book.json`; `books/<md5>.epub`; tags read back with `ffprobe` (title, artist, album, track) |
| Title mapping: a TXT sent through `POST /api/jobs`, and one dropped in `inbox/` | Library title is the file name (`Hành trình của tôi`), with the MD5 shown below it. Before the fix both showed the MD5 as the title |
| EPUB with an empty title tag | Used the file name. Before the fix the job crashed |
| Browser, Rename on a library card | The title and author changed on the card; tags in every chapter file changed, audio length unchanged (`ffprobe`) |
| Container, `docker kill` during chapter 3, then `docker start` | Chapters 1 and 2 reused, 3 and 4 rendered, 4 files in the folder |

Voice worker, run natively on the Mac (worker on port 8780, server on 8798, no Docker). The worker and server were separate processes:

| Check | Result |
|---|---|
| Settings tab: pick a worker, wrong token, right token, unreachable address | "the worker rejected the token"; "Connected in 11 ms. Models loaded on the worker: turbo not loaded yet, nano ready"; "cannot reach … Is the worker running, and is the address right?" |
| Save the worker, open Convert | The voice list (11 voices) and a voice sample (6.6 s of audio in 1.8 s) came from the worker: its log shows the `/synthesize` call |
| Convert a 2-chapter book with the worker | 17 sentences synthesized on the worker, 2 chapter files in the library, identical file names to the earlier local run. The server process used 69 MB; the worker 451 MB |
| Switch back to This server | The server loaded the model itself (memory rose to 695 MB) and made the next sample locally; the worker's call count did not change |
| Restart the server | It came back on the saved worker engine |
| Kill the worker during chapter 2 of a 4-chapter book | After the retries (about 20 s) the job stopped with "cannot reach the voice worker at http://127.0.0.1:8780". Chapter 1 stayed. After restarting the worker, Retry reused chapter 1 and finished chapters 2 to 4 |
| Worker off: voice list and sample | The list answered at once (2 ms) from memory; an uncached sample returned 502 with the reason after 20 s |
| Phone-like worker (`WORKER_ACCEPTS=phonemes`) with the server in a slim environment (no ONNX Runtime) | The server connected, listed 11 voices, refused cloning with a clear message, and converted the 2-chapter book in 16 s with 17 `/infer` calls and no `/synthesize` call. Same 74 s of audio as the local run |
| Nano loop in Kotlin against golden files from Python, on a Mac JVM and on a Pixel 8 Pro | Identical output on both (differences 0.00, correlation 1.00000). Pixel, 7.4 s chunk, 4 threads, fp32: RTF 0.48 first, 0.97 settled; int8: 0.19 first, about 0.6 settled over 90 runs. The phone throttled from about 2.1 to 2.7 GHz down to about 1.1 to 1.3 GHz within a minute |
| Calibre tab against a stand-in Calibre-Web (the test suite's fake served over HTTP, with its own login) | Settings: Test said "Connected to Calibre-Web. Sections: Recently added, Authors, Series", Save kept it. The tab opened on Recently added with 3 books and a Load more that fetched the third. Search found the book; the trail's Library link showed the sections. Convert EPUB downloaded the sample book, opened step 2 with 12 chapters, and its MD5 matched the book already in the library |
| English book end to end: upload an English 2-chapter EPUB, page picks the English model and voices, narrator M2 and dialogue F2, Convert | The page switched the model and hid Expressiveness by itself. The job finished with 2 chapter files (70 s and 50 s). I transcribed the two files with Whisper and compared them with the book text: 2.3 % and 4.2 % word errors (the differences were "Okafur" for "Okafor" and "gray" for "grey"). `Mr.`, `Dr.`, `9:30 a.m.` and `e.g.` were read inside their sentences |
| Passive worker with empty model folders (`HF_HOME` pointing at an empty directory), this server pointed at it alone | The worker started with nothing loaded and every model `absent`. Settings showed "not downloaded" with Download buttons for it and "on disk" for this server. Converting an English book: "Run on" said the worker "does not have the English · Supertonic model yet: will download it (about 390 MB) first, then start"; the worker downloaded it (about 15 s here) into its own folder and the job finished. The other models stayed absent |
| The same worker, a Nano job (Nano was absent) sampled every second | "asking the worker for the Vietnamese · Nano model", then "downloading … (26 of about 280 MB)", "(182 of about 280 MB)", "(281 of about 280 MB)", then rendering |
| Unit and integration tests | 273 pass natively (2 PDF tests skip without poppler). They cover the worker's auth and protocol, the retry rules, clone sync, the machine list and API, chapters shared across machines (parallel work, a machine failing before or during a chapter, everyone failing, cancel, a machine still loading, resume), making a machine ready (model, preset and cloned voices), cover finding, a whole book shared between this server and a worker, what each machine has of each model, the worker's `/models` and `ensure`, the server waiting through a download and reporting it, older workers, the English model (with a stand-in for it), picking it by language for the page, scripts and the inbox, English abbreviations, and the Calibre-Web client and API (feeds, search, paging, every failure message, path safety, downloads, the saved password) |

Several machines on one book, run natively on the Mac with the real Nano model: this server, a full worker on port 8781 and a phone-like worker (`WORKER_ACCEPTS=phonemes`) on 8782, all three on the same CPU, driven through the page:

| Check | Result |
|---|---|
| Settings: add two workers, Test one with the right token and one with a wrong token | The right one showed "Online · 78 ms" and filled in its reported name; the wrong one said "the worker rejected the token" |
| Convert, step 3 | "Run on" listed all three machines with dots. This server said "nano model ready"; both workers said "will download and load the nano model first". The confirm dialog listed "Run on: This server, Mac worker A, Phone-like B" |
| Convert 5 chapters (three of about 12,000 characters, two tiny) | The Jobs tab showed all three machines working at once on three different chapters, the longest first, with "loading the Nano model (the first time this downloads it)" on the workers before that |
| Kill the full worker while it was on Chương IV | It was dropped after the retries ("Mac worker A dropped out: cannot reach the voice worker at http://127.0.0.1:8781 (ConnectError)"). Its chapter went back and was made by the phone-like worker. The job finished with 5 chapters: this server 3, phone-like worker 2 |
| Files | 5 `.m4a` files of 15 s, 17 s and about 12 minutes each in `library/<md5>/`, plus `cover.jpg` |

I have not listened to the audio from the second machine, and with three processes on one CPU this shows that work is shared and failures are survived, not how much faster it is. Cloned voices going to a worker were tested in the unit tests (with a fake model), not live here.

Covers: the library now takes the cover an EPUB declares (EPUB 3 `cover-image`, EPUB 2 `<meta name="cover">`, the guide's cover page and the picture inside it), and only then guesses by file name; it checks the bytes, not the extension, and shows no cover rather than a random picture. A PDF uses its first page (through `pdftoppm`; poppler is not installed on this Mac, so that was tested with a stand-in script, not poppler itself). The sample book's declared cover is itself a text page, and that is what the library shows for it. The cover also appears right after you upload, before converting.

The Calibre tab was also run against a real Calibre-Web instance on a local network (through the app's own saved connection, read-only): the catalogue parsed (26 books on the first page, titles, formats, cover and download links), 8 books were downloaded and their covers compared with the ones Calibre-Web serves (which showed that Calibre's covers can differ from the ones inside the EPUBs and led to the cover import described above), and 3 books were imported with Calibre-Web's cover on a scratch copy of the data. Not tried against a real instance: search, paging beyond the first page, and the Preview button opening the page (the embedded browser blocks pop-ups).

The English model's own test, which loads the real model, is off by default: `RUN_ENGINE_TESTS=1 pytest tests/test_english_real.py`. I did not time English on an N100 and did not test it in Docker; the Docker image installs it from `requirements-engine.txt`, untested like the rest of the image.

Not tested for the worker feature: a worker running as an Android app, Docker (you asked me to skip it; see the note below), a phone, a worker on another machine over Wi-Fi, HTTPS, and a worker with Turbo.

Checked on earlier versions of the pipeline and not repeated since the library change:

| Check | Result |
|---|---|
| Processors on a real 10-chapter Vietnamese EPUB (`Hành Trình Về Phương Đông`, chapters in `<div>` markup) | Table of contents found 12 sections (10 selected), one chapter per file 13, headings 3, title lines 12, equal parts 21. A voice sample played in 2 s |
| Voices after a restart | 11 Nano voices answered in 6 ms with `ready: false` while Turbo loaded in the background; Turbo's 25 voices answered in 2 ms after its first load |
| FB2 through Calibre in the container | 2 chapters from every processor, converted file kept for later previews. Without Calibre the job ends in `error` saying so |
| Cloning from a 6 s clip, Nano then Turbo | enrolled, listed, not silent in both models, survives a restart |
| Dialogue voice, 1-chapter book | 3 of 10 clips detected as dialogue |
| Speed | Nano in the container: 226 s of audio in 137 s (RTF 0.61); natively about 3x faster (RTF 0.14 to 0.18). Turbo natively RTF 0.27 with 10 threads, 0.60 with the runtime default |

Not tested: a NAS, any CPU other than this Mac, Audiobookshelf scanning the new `<md5>/` folders (needs a login created in its UI; I expect it to read them as one multi-file book from the tags but have not seen it), cloning quality by ear, and a book whose chapter text is identical in two chapters with the same title.

**Docker.** `Dockerfile.server` (2.4 GB with Calibre, 0.7 GB with `WITH_ENGINE=0 WITH_CALIBRE=0`) and `Dockerfile.worker` (1.5 GB) were built on the M1 Pro Mac (linux/arm64) and both compose files validated. The two containers ran together on one Docker network: both reported healthy, the server (pointed at the worker container with `TTS_BACKEND=remote`) converted an English book, the worker container downloaded the English model on demand into its volume (about 390 MB), and two chapter files appeared in the server's `data/library`. Inside the server image `ebook-convert`, `pdftotext`, `pdftoppm` and `ffmpeg` are present. Not tried: an amd64 build (the NAS), the test suite inside the image (the image leaves out `tests`), and Audiobookshelf.

## Limits

- A chapter's identity is its title plus text. Split the same book a different way and chapters whose text differs are treated as new, so both sets of files will sit in the folder; delete the ones you do not want from the Library.
- One book renders at a time. TTS already uses the CPU; parallel jobs would only fight for it.
- Languages: Vietnamese (VieNeu Nano and Turbo) and English (Supertonic). Mixed Vietnamese and English text in one book is read by whichever model the book was given; the Turbo model code-switches per upstream, but I have not tested it. Other languages need a mapping to Supertonic's other languages or another engine behind the `Engine` protocol in `app/tts.py`.
- Cloned voices are stored as reference clips in `data/voices/` and enrolled into each model when it loads. I checked that the output is not silent, not that it sounds like the speaker.
- Dialogue detection is quote and dash based. It does not know who is speaking, so there is one dialogue voice per book, not one per character.
- VieNeu can add an audio watermark, but only if the optional `resemble-perth` package (with PyTorch) is installed (`pip install vieneu[watermark]`). This service does not install it, and I confirmed it is absent here, so the audio is not watermarked. If you add that package, the watermark is applied.
- Use books you own in DRM-free form. Audio for personal use.
