# Configuration

Two kinds of settings: environment variables read when the service starts, and settings you change in the page, which
are saved on disk and win over the environment afterwards.

## Server environment variables

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `/data` in the Docker images; otherwise `./data` in the folder you start it from | Where everything is kept (see below). When `DATA_DIR` is not set and there is no writable `/data` (a native run on a Mac, Windows or Linux machine), the data goes in `./data` |
| `PORT` | `8765` | Port of the page and API |
| `TTS_MODEL` | `nano` | The Vietnamese model a Vietnamese book starts with: `nano` or `turbo` |
| `TTS_ENGLISH_MODEL` | `english` | The model an English book starts with. `none` keeps `TTS_MODEL` for every book |
| `TTS_ENGLISH_VOICE` | model default (`F1`) | Default narrator for English books |
| `TTS_VOICE` | model default | Default narrator for books started without an explicit voice, a preset voice name such as `Mỹ Duyên` |
| `TTS_DIALOG_VOICE` | none | Default dialogue voice. Unset means one voice for everything |
| `TTS_TEMPERATURE` | `0.8` | Expressiveness of the Vietnamese voices; higher is more expressive and less steady |
| `TTS_PRECISION` | `fp32` | Turbo only: `fp32` or `int8` |
| `TTS_THREADS` | all CPUs visible | ONNX threads per model. The runtime's own default was twice as slow in one test |
| `TTS_DEVICE` | `auto` | Hardware for the models: `auto` (a GPU for the models that support one, the CPU for the rest), `cpu`, `cuda`, `directml`, `coreml`, `openvino`. Also the flag `python -m app --device cuda`. A device that is not available stops the start with a message. See [Using a GPU](workers.md#using-a-gpu) |
| `READ_TITLES` | `1` | Read each chapter title before its text (`0` turns it off) |
| `PRELOAD_MODELS` | `none` | Models to load at start, comma separated (`nano`, `turbo`, `english`). By default nothing is loaded or downloaded until needed. An unknown name stops the start |
| `TTS_BACKEND` | `local` | First-start machine list: `local` (this server) or `remote` (one worker, this server off). Replaced by whatever you save in Settings |
| `TTS_WORKER_URL`, `TTS_WORKER_TOKEN` | none | The worker for `TTS_BACKEND=remote` |
| `SCAN_INTERVAL` | `10` | Seconds between looks at the drop folder |
| `SETTLE_SECONDS` | `5` | A file in the drop folder must be unchanged this long before it is queued |
| `HF_HOME` | Hugging Face default (`/data/cache/hf` in Docker) | Where models are downloaded |
| `SUPERTONIC_CACHE_DIR` | `$HF_HOME/supertonic3` when `HF_HOME` is set, else the library default (`~/.cache/supertonic3`) | Where the English model is kept |

Uploads can override model, voice, dialogue voice, expressiveness and title reading for one book.

## Worker environment variables

`WORKER_PORT` (8780), `WORKER_TOKEN`, `WORKER_NAME`, `WORKER_ACCEPTS` (`both` or `phonemes`), `WORKER_MODELS` (`all`: the models the worker serves), `PRELOAD_MODELS` (`none`),
and `DATA_DIR`, `HF_HOME`, `SUPERTONIC_CACHE_DIR`, `TTS_THREADS`, `TTS_PRECISION`, `TTS_DEVICE` as above. See [Voice workers](workers.md).

## The data folder

Everything the service keeps is under `DATA_DIR`:

| Path | What | Back it up? |
|---|---|---|
| `library/<md5>/` | Chapter audio, cover, `book.json` | **Yes**: this is the product |
| `books/<md5>/<name>` (and `.cover/`) | Your original book files, and a cover chosen for them | **Yes**: they let you re-convert |
| `config/engine.json` | The machine list (mode 600; contains worker tokens) | Yes |
| `config/calibre.json` | Calibre-Web address, username, password (mode 600) | Yes |
| `config/pronunciations.txt` | Your word replacements | Yes |
| `voices/` | Reference clips of cloned voices, and an index | Yes if you cloned any |
| `jobs.sqlite` | The job queue and history | Optional |
| `inbox/` | The drop folder | No |
| `uploads/<id>/` | Files uploaded but not yet converted | No (swept after 24 h) |
| `cache/samples/` | Voice sample MP3s | No |
| `cache/m4b/` | Built M4B downloads | No |
| `cache/voices.json`, `cache/voices-<id>.json` | Last known voice lists | No |
| `cache/hf/` (Docker) | Downloaded models | No (they download again) |

## Files that the page writes

**`config/engine.json`**

```json
{
  "local_enabled": true,
  "workers": [
    {"id": "a990cd00", "name": "Laptop", "url": "http://192.168.1.20:8780", "token": "secret", "enabled": true}
  ]
}
```

A file from the earlier single-worker version (`{"backend", "worker_url", "worker_token"}`) is still read: `local`
becomes "this server on, no workers"; `remote` becomes "this server off, one worker".

**`config/calibre.json`**: `{"url", "username", "password"}`.

**Browser storage.** The page remembers, per browser, which machines you ticked under "Run on" (`localStorage` key
`runOn`). Nothing else.

## Pronunciations

`config/pronunciations.txt`, one `from=to` per line. Matching is whole-word and case-insensitive, longest match first;
lines starting with `#` are ignored. Read at the start of every job.

```
iPhone 15=ai phôn mười lăm
# acronyms
NASA=na xa
```

## Docker

**Server** (`Dockerfile.server`, `docker-compose.yml`). The compose file sets `TTS_MODEL`, `TTS_TEMPERATURE` and
`READ_TITLES`, mounts `./data` as `/data`, and leaves `PRELOAD_MODELS` as a commented example. The image sets
`DATA_DIR=/data`, `HF_HOME=/data/cache/hf` and `PORT=8765`. Build arguments: `WITH_ENGINE=0` (no speech packages; for
a server that only uses workers) and `WITH_CALIBRE=0` (no `ebook-convert`). The health check calls `/healthz`.

**Compose variables.** Both compose files read a `.env` file next to them: `SERVER_PORT` (host port of the server, default
8765), `SERVER_DATA` (host folder mounted as `/data`, default `./data`), `WORKER_PORT` (default 8780), `WORKER_TOKEN`
(required for the worker), `WORKER_NAME`. Both files join a network named `ebook2audio-net`, so a server and a worker
started on the same machine reach each other as `http://voice-worker:8780`. Keep `.env` private: it holds the token and
is excluded from git. `.env.example` shows the variables with placeholders; copy it to `.env`.

**Worker** (`Dockerfile.worker`, `docker-compose.worker.yml`). The image sets `DATA_DIR=/data`, `HF_HOME=/data/cache/hf`
and `WORKER_PORT=8780`; `WORKER_TOKEN` is required by the compose file, and `WORKER_NAME` and `TTS_THREADS` are
optional. `/data` is a named volume (`worker-data`) so downloaded models survive a new container. The health check
calls `/health` with the token.
