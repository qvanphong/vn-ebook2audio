# Voice workers and sharing a book

Speech is the heavy part of the work. By default this server makes it itself. You can add other computers as **voice
workers** and let several machines share each book. The library, files, page and job queue always stay on the main
server; a worker only turns text into audio and sends it back.

## The machine list

**Settings → Voice machines** holds the list: **This server** (always present; greyed out if the speech packages are
not installed here) and any number of workers. For each worker:

| Field | Meaning |
|---|---|
| Use | Whether it is included by default in new conversions |
| Name | A label; **Test** fills in the name the worker reports |
| Address | `http://host:8780` (a bare `host:8780` is accepted) |
| Token | The worker's shared secret, if it has one. Blank keeps the saved one; "forget the saved token" removes it |

**Test** checks that the address answers, the token is accepted, the protocol is supported, and shows which models the
worker has. **Save** applies the list, which is stored in `data/config/engine.json` (mode 600). The list can be changed
at any time, even during a conversion: a running conversion keeps the machines it started with.

At least one machine must be switched on. On the first start the list comes from the environment: `TTS_BACKEND=local`
(the default) means just this server; `TTS_BACKEND=remote` with `TTS_WORKER_URL` (and `TTS_WORKER_TOKEN`) means one
worker and this server off. After you save in the page, the saved list wins.

The voice list, voice samples and cloning come from the first machine that is switched on (this server if it is on,
otherwise the first worker).

## Choosing machines for a book

In Convert, step 3 shows **Run on** when there is more than one machine. Each line has a green or red dot (does it
answer?) and a note about what that machine will do for this book. Tick the machines to use. Your ticks are
remembered in the browser. A machine switched off in Settings is still listed and can be ticked for one book.
A machine is greyed out, with the reason, when it is offline, cannot run the chosen model, or cannot use a cloned voice
that you chose.

## How a book is shared

When a job starts, every chosen machine becomes a **lane** and starts getting ready at the same time. A lane takes the
next unfinished chapter as soon as it is free, so a faster machine does more of the book. With several machines the
longest chapters go out first, which keeps the last chapter from being a long one on a slow machine; with one machine
chapters go in book order.

- A machine that is still loading a model does not hold anyone up; the others start and it joins when ready.
- A chapter is made entirely by one machine. Each finished chapter is encoded and added to the library by the main
  server, whichever machine made it.
- A lane that fails (offline, missing model or voice, cannot load) is dropped and its chapter goes back to the queue
  for the others. The job line says which machine dropped out and why. If every machine fails, the job stops with each
  reason; finished chapters stay in the library and **Retry** continues from there.
- An error that is not a machine problem (for example a bug while making audio) stops the whole job.
- If a chapter that was already finished is picked again (a retry, or *skip* not chosen), it is reused unless the job
  was told to replace it.
- A chapter in progress when you press **Cancel** is lost; every lane stops after its current sentence.

## Making a machine ready: the passive worker

A worker needs no model configured. It starts empty (`PRELOAD_MODELS` defaults to `none`) and the server tells it what
a job needs. For each machine, before its first chapter:

1. The server asks the worker for the model (`POST /models/{model}/ensure`).
   - Already loaded: it is used at once.
   - On disk: the worker loads it.
   - Never had it: the worker **downloads it first**, then loads it. The job starts after that.
2. The Jobs tab shows each stage: "asking the worker for the … model", "downloading … (182 of about 280 MB)",
   "loading", then the chapter it is on.
3. The voice named in the request is checked. A preset voice the worker lacks is reported by name and the worker
   drops out; a cloned voice it never had is uploaded to it.

A worker that cannot run the model (the package is not installed, or it is a phone that only runs Nano) says so and
drops out, and the others carry on. A worker without the newer endpoints (protocol 2) is waited for the old way, by
asking for its voices, which makes it load the model.

You can do step 1 ahead of time in **Settings**: each machine lists every model as `ready`, `on disk`, `downloading`,
`not downloaded` or `cannot run here`, with a **Download** or **Load** button (`POST /api/workers/<id>/models/<model>`).
The "of about N MB" figure is a rough size from the catalogue, and the amount arrived is counted from the worker's own
cache folder, so the count can pass 100%.

Nothing unloads a model to free memory: a worker asked for all three keeps all three loaded.

## Running a worker

On the helping machine (Python 3.12 and ffmpeg installed), in this project:

```bash
cd nas-audiobook
pip install -r requirements.txt -r requirements-engine.txt
WORKER_NAME="Laptop" WORKER_TOKEN=choose-a-secret python -m app.worker
```

It listens on port 8780. Add its address and token in Settings and press Test.

Or as a container, with the image built from `Dockerfile.worker`:

```bash
cd nas-audiobook
WORKER_TOKEN=choose-a-secret WORKER_NAME=Laptop docker compose -f docker-compose.worker.yml up -d --build
```

Models are downloaded on demand into the `worker-data` volume. Tested: the worker container and the server container
on one Docker network, the server pointed at `http://<worker container>:8780`; converting an English book made the
worker download its model (about 390 MB), then render both chapters.

| Variable | Default | Meaning |
|---|---|---|
| `WORKER_PORT` | `8780` | Port |
| `WORKER_TOKEN` | none | If set, every request needs `Authorization: Bearer <token>`. **Without it anyone who can reach the port can use the worker, so set one** |
| `WORKER_NAME` | none | A name reported in `/health` |
| `WORKER_ACCEPTS` | `both` | `phonemes` makes the worker behave like a phone (see below) |
| `PRELOAD_MODELS` | `none` | Models to load at start, comma separated, if you want them ready before the first request |
| `DATA_DIR` | `/data` | Where it keeps cloned voices and its voice lists |
| `HF_HOME`, `SUPERTONIC_CACHE_DIR` | defaults | Where models are downloaded |
| `TTS_THREADS`, `TTS_PRECISION` | as for the server | How the models run |
| `TTS_DEVICE` | `cpu` | `cpu`, `auto`, `cuda`, `directml`, `coreml` or `openvino` (see [Using a GPU](#using-a-gpu)); the command-line flag `--device` overrides it |

The worker needs ffmpeg for cloned voices. A worker is not a server: it has no page, no library and no queue.

### Network and security

The server must be able to reach the worker's address; there is no discovery. Traffic is plain HTTP unless you put the
worker behind HTTPS yourself, so the token and the text of your books cross the network unencrypted. Use it on a
network you trust, or put both behind a VPN or a TLS proxy.

### If a worker goes away

Each request to a worker is retried for about 20 seconds (four tries with growing pauses) before that machine is
dropped from the job. Start it again and press **Retry** on the job. With the worker off, the voice list still shows
from the saved copy, and a sample fails with a clear message after the same 20 seconds.

## Using a GPU

By default the models run on the CPU. To use a GPU, set the device on the machine that runs the models (a worker, or
this server for "This server"):

| `TTS_DEVICE` (or `--device`) | Uses | ONNX Runtime package it needs |
|---|---|---|
| `cpu` (default) | The CPU | `onnxruntime` |
| `auto` | A GPU if one is available (CUDA, then DirectML), otherwise the CPU | whichever you installed |
| `cuda` | An NVIDIA GPU | `onnxruntime-gpu`, plus CUDA 12 and cuDNN 9 on the machine |
| `directml` | Any GPU on Windows (NVIDIA, AMD, Intel) | `onnxruntime-directml` |
| `coreml` | Apple silicon | the ordinary macOS `onnxruntime` |
| `openvino` | Intel | `onnxruntime-openvino` |

```bash
TTS_DEVICE=cuda python -m app.worker           # or: python -m app.worker --device cuda
```

The flag only changes which hardware the model libraries are told to use; the ONNX Runtime package decides what is
possible, and the ordinary `onnxruntime` package can only use the CPU. The two packages cannot be installed together:
`pip uninstall onnxruntime` first, then `pip install onnxruntime-gpu==1.22.0` (or `onnxruntime-directml`).

**Asking for a device that is not there is an error at start**, with a message naming the package to install, so a
worker never carries on silently on the CPU after you asked for a GPU. `auto` is the forgiving choice.

**The GPU is tried before it is relied on.** At start, a GPU choice is tested on a tiny model in a separate process,
because a missing or broken GPU driver can crash the process outright instead of raising an error. If the test fails:
an explicit choice (`cuda`, `directml`, ...) stops the start with the reason ("CUDAExecutionProvider could not start:
the test process crashed (signal 11), which usually means no working GPU driver"), and `auto` logs a warning and uses
the CPU. With `restart: unless-stopped` a container that cannot get its GPU therefore restarts in a loop with that
message in its log, which is the signal to fix the driver or the container's GPU access.

**Docker, NVIDIA:** `Dockerfile.worker-cuda` (linux/amd64) is built on an NVIDIA CUDA runtime image with
`onnxruntime-gpu`, and `docker-compose.worker-gpu.yml` starts it with the GPU attached and `TTS_DEVICE=cuda`:

```bash
WORKER_TOKEN=choose-a-secret docker compose -f docker-compose.worker-gpu.yml up -d --build
```

Docker needs access to the GPU: the NVIDIA Container Toolkit on Linux, or Docker Desktop with the WSL 2 backend and a
current NVIDIA driver on Windows. **Docker on a Mac cannot use a GPU, and AMD or Intel GPUs on Windows cannot be used
from a Linux container**; for those, run the worker directly on Windows with `onnxruntime-directml` and
`TTS_DEVICE=directml`.

**Checking that it is really in use.** Settings → Voice machines shows each machine's hardware next to its status:
"CPU", "GPU (CUDA)", or in red "asked for cuda but running on the CPU". The same information is in the worker's
`/health` (`device`). It is confirmed only after a model has loaded, because that is when the sessions are created.

**A model that fails on the GPU falls back to the CPU.** When a GPU is requested, each model is given a short first run
as soon as it loads. A GPU provider can accept a model and then fail when it runs, and a model that fails this check is
loaded again on the CPU, with a warning in the log and the red status above. The other models keep trying the GPU.

What was and was not tested: the device setting, the provider rewriting, the status, the self-test and the fallback are
covered by tests with a stand-in for ONNX Runtime, and the self-test's hand-written model runs on the real runtime. On an
Apple silicon Mac, `coreml` was tried with the real Nano model: CoreML accepted the model and then failed on its first
run, the fallback reloaded it on the CPU, and speech worked. The CUDA image (`Dockerfile.worker-cuda`, about 5 GB) was
built, contains only `onnxruntime-gpu` 1.22.0 and lists the CUDA provider; run on a Mac (an emulated amd64 container with
no GPU), `TTS_DEVICE=cuda` stopped at start with the message above, `auto` used the CPU, and the English model spoke
normally on the CPU path. **No real GPU was available, so CUDA and DirectML are untested on hardware, and no speed-up
was measured.** Do not assume a
GPU is faster for every model: these models are small, and the Turbo model runs many tiny steps for each audio frame,
which a GPU handles less well than a large batch.

## Workers that only take phonemes

A small worker such as a phone cannot run this service's text code (normalising numbers, chunking, phonemising). It
advertises `capabilities.synthesize: false`, and the main server then does that text work itself and sends one chunk of
phonemes at a time to `POST /infer`, then joins the audio. That needs VieNeu's text code on the main server, which
needs no ONNX Runtime and no PyTorch:

```bash
pip install -r requirements.txt
pip install --no-deps vieneu==3.8.3 && pip install sea-g2p
```

(On the Mac this gave a 127 MB environment instead of 724 MB.) Only the Nano model can run this way, and voice cloning
is off while such a worker is the voice-list source. Try it without a phone by starting the Python worker with
`WORKER_ACCEPTS=phonemes`. See [Phone worker](phone-worker.md).

## Worker protocol

A worker is anything that implements the endpoints in [HTTP API](api.md#voice-worker-protocol-version-3). The current
protocol is **3**; the server also understands 1 and 2. Protocol 3 added `GET /models` and
`POST /models/{model}/ensure`; protocol 2 added `POST /infer` and `capabilities`.
