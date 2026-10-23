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

- **Several books at once.** A machine works on one book at a time and keeps it as long as that book has chapters for
  it. When that book has none left for the machine (the last chapters are on slower machines) the machine moves on to
  the next queued book while the others finish the first. Example: chapters 1 and 2 of book A go to a laptop and a
  desktop; the desktop finishes chapter 1 and book B, queued for the desktop, starts there while the laptop is still
  on chapter 2. The queue is the order the jobs were created in:
  - A queued job starts as soon as at least one machine it may use is free. A younger job that needs only a free
    machine starts before an older one that is waiting for a busy machine.
  - A machine goes to the older job first. If a machine hands a chapter back to an older job (another machine
    failed) while a younger job is using this one, the younger job lets go after its current chapter.
  - A job that started on some of its machines shows the others as "waiting for another book to finish with this
    machine" and joins on them when they are free. Cancelling stops that wait.
  - A job whose book is already complete needs no machine, but still waits for one to be free before it is looked at.
- A machine that is still loading a model does not hold anyone up; the others start and it joins when ready.
- A chapter is made entirely by one machine. Each finished chapter is encoded and added to the library by the main
  server, whichever machine made it.
- A machine that fails never stops a book the others can finish. Its chapter goes back to the queue for the other
  machines and the job line says which machine dropped out and why. What happens next depends on why:
  - **Out of reach** (switched off, asleep, off the network, the worker stopped): the machine is tried again every 20
    seconds while the book goes on, and takes chapters again as soon as it answers. A short outage (about 20
    seconds, while a request is retried) is not even noticed. A machine that does not answer a connection within 5
    seconds counts as out of reach.
  - **Cannot do this job** (it lacks a model or a preset voice, does not serve the model, refuses the token): it stays
    out of this book; the others carry on.
  - **No machine left.** If every machine that was working on the book is out of reach, the book does **not** fail. It
    goes back to **queued** with the note "waiting for a machine: …", keeps the chapters it finished, and is started
    again 20 seconds later, on whichever machines can be reached then (including ones that were not in use before).
    Cancel it if you no longer want it. If instead every machine said it cannot do the job, the book stops with an
    error giving each reason; finished chapters stay in the library and **Retry** continues from there.
  - A machine that disappears without closing the connection (power cut, cable pulled) is noticed only when its
    request times out, which can take up to five minutes for a chapter it was in the middle of.
- An error that is not a machine problem (for example a bug while making audio) stops the whole job.
- If a chapter that was already finished is picked again (a retry, or *skip* not chosen), it is reused unless the job
  was told to replace it.
- A chapter in progress when you press **Cancel** is lost; every lane stops after its current sentence.

## A worker owns its models

The server never tells a worker to download anything, and never picks its device. A worker decides two things for itself.

**Which models it serves.** `WORKER_MODELS` (or `--models`) lists them, for example `nano,english`; the default is all
three. A worker only takes part in a book whose model it serves, and Convert greys it out otherwise ("does not serve the
… model"). A model it does not serve is never downloaded, even if asked for.

**How and when it gets them.** A served model is downloaded and loaded by the worker itself, either at start (the
models named in `PRELOAD_MODELS`, which must be among the served ones) or the first time a request needs it. In the
second case, before a machine's first chapter:

1. The server asks the worker for the model's voices, which is what makes a worker that serves the model start getting
   it. Already loaded: it is used at once. On disk: the worker loads it. Never had it: the worker **downloads it
   first**, then loads it. The job starts after that.
2. The server only watches `GET /models` and the Jobs tab shows each stage: "waiting for the worker to get the … model",
   "the worker is downloading … (182 of about 280 MB)", "the worker has the … model and is loading it", then the
   chapter it is on.
3. The voice named in the request is checked. A preset voice the worker lacks is reported by name and the worker
   drops out; a cloned voice it never had is uploaded to it.

A worker that cannot run the model (the package is not installed, or it is a phone that only runs Nano) says so and
drops out, and the others carry on. A worker older than protocol 3 is waited for the same way.

**Settings** lists each machine's models as `ready`, `on disk`, `downloading`, `not downloaded`, `not served by this
worker` or `cannot run here`, with the device each runs on. Only **This server** has **Download** and **Load** buttons: a
worker's models are changed on the worker (`WORKER_MODELS`, `PRELOAD_MODELS`), and asking the server to fetch one for a
worker is refused (`POST /api/workers/<id>/models/<model>` answers 409). The "of about N MB" figure is a rough size
from the catalogue, and the amount arrived is counted from the worker's own cache folder, so the count can pass 100%.

Nothing unloads a model to free memory: a worker asked for all three keeps all three loaded.

## Running a worker

On the helping machine (Python 3.12 and ffmpeg installed), in this project:

```bash
cd nas-audiobook
python3.12 -m venv .venv && . .venv/bin/activate     # optional, but keeps it tidy
python -m pip install -r requirements-worker.txt     # everything a worker needs, in one file
WORKER_NAME="Laptop" WORKER_TOKEN=choose-a-secret python -m app.worker
```

On Windows (PowerShell), the same steps are:

```powershell
cd nas-audiobook
py -3.12 -m venv .venv; .\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-worker.txt
$env:WORKER_NAME = "Windows PC"; $env:WORKER_TOKEN = "choose-a-secret"
python -m app.worker
```

Install with `python -m pip` so the packages go into the same Python that runs the worker. Installing with a different
`pip` than the `python` you start it with is the usual reason for "No module named 'fastapi'". The Windows steps were
not run on a Windows machine, and some of the speech packages' dependencies may need a C++ compiler there if no ready
wheel exists. Windows Firewall must allow the port (TCP 8780).

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
| `WORKER_MODELS` | `all` | The models this worker serves, comma separated (`nano`, `turbo`, `english`), or `all`. Also `--models` |
| `PRELOAD_MODELS` | `none` | Models, among the served ones, to download and load at start instead of on first use |
| `DATA_DIR` | `/data` in Docker, else `./data` | Where it keeps cloned voices and its voice lists |
| `HF_HOME`, `SUPERTONIC_CACHE_DIR` | defaults | Where models are downloaded |
| `TTS_THREADS`, `TTS_PRECISION` | as for the server | How the models run |
| `TTS_DEVICE` | `auto` | `auto`, `cpu`, `cuda`, `directml`, `coreml` or `openvino` (see [Using a GPU](#using-a-gpu)); the command-line flag `--device` overrides it |

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

The worker picks the device for each model. By default (`auto`) it uses a GPU it finds (CUDA, then DirectML) for the
models that can use one, and the CPU for the rest, so nobody has to know which model runs where. Which devices a model
can use is in the catalogue: **Nano is CPU only**, **Turbo** uses CUDA through PyTorch (see below), **English** may use CUDA,
DirectML or CoreML through ONNX Runtime. A machine with an NVIDIA GPU therefore runs Turbo on the GPU and Nano on the CPU at the same time,
and a Mac runs both Vietnamese models on the CPU. A model that fails its first run on the GPU is loaded again on the
CPU. Settings shows the device each model really runs on. Only English on CoreML has been run on real hardware here, and it was *slower* than the CPU (RTF 0.46 against 0.31 on an M1 Pro, so a GPU is not automatically faster for these models);
CUDA and DirectML for Turbo and English are tried at run time and fall back, not tested.

To force the choice, set the device on the machine that runs the models (a worker, or this server for "This server"). A
named device that is not available stops the start with the reason; the models that cannot use it still run on the CPU:

| `TTS_DEVICE` (or `--device`) | Uses | ONNX Runtime package it needs |
|---|---|---|
| `auto` (default) | A GPU if one is available (CUDA, then DirectML), otherwise the CPU | whichever you installed |
| `cpu` | The CPU for every model | `onnxruntime` |
| `cuda` | An NVIDIA GPU | for English `onnxruntime-gpu`, plus CUDA 12 and cuDNN 9 on the machine; for Turbo the CUDA build of PyTorch (`requirements-engine-torch-cuda.txt`) |
| `directml` | Any GPU on Windows (NVIDIA, AMD, Intel) | `onnxruntime-directml` |
| `coreml` | Apple silicon | the ordinary macOS `onnxruntime` |
| `openvino` | Intel | `onnxruntime-openvino` |

```bash
TTS_DEVICE=cuda python -m app.worker           # or: python -m app.worker --device cuda
```

**NVIDIA GPU on Windows or Linux, running directly (not in Docker):** the worker needs the GPU build of ONNX Runtime and
the CUDA and cuDNN libraries.

```bash
python -m pip install -r requirements-worker.txt
python -m pip uninstall -y onnxruntime
python -m pip install --force-reinstall "onnxruntime-gpu[cuda,cudnn]==1.22.0"     # for the English model on CUDA
python -m pip install -r requirements-engine-torch-cuda.txt                      # for Turbo on CUDA
```

**Turbo uses PyTorch on the GPU, not ONNX Runtime.** VieNeu has two Turbo engines: an ONNX one that only runs on the
CPU, and a PyTorch one with CUDA graphs and batching that is meant for NVIDIA GPUs (its authors measure Turbo at about
0.1 seconds of work per second of audio on an RTX 3060, against 0.55 to 0.62 on a laptop CPU). The worker picks between
them by itself: if PyTorch can use CUDA on the machine, Turbo runs on the GPU through PyTorch; otherwise on the CPU
through ONNX, whatever ONNX Runtime package is installed. ONNX Runtime with CUDA (`onnxruntime-gpu`, cuDNN) is now only
for the English model; Nano never uses a GPU. So for Turbo on an NVIDIA GPU:

```bash
python -m pip install -r requirements-engine-torch-cuda.txt
```

That installs the CUDA build of PyTorch (about 2.5 GB, with its own CUDA and cuDNN) and the `transformers` version
VieNeu pins; do not install `vieneu[cuda]`, which pulls in more than the worker needs. PyTorch's Turbo files
(about 340 MB) are downloaded by the first Turbo request, in addition to the ONNX ones if the CPU engine is ever used.
At start the worker tries PyTorch on the GPU in a child process; with `auto` a failure is a log line and Turbo runs on
the CPU, with `--device cuda` it is a warning (the worker still starts, because English may have ONNX CUDA). If Turbo
fails its first run on the GPU it is loaded again on the CPU, and Settings shows `CUDA` or `CPU` next to it with the
reason on hover. I ran the PyTorch engine on this Mac's CPU only (it loads, lists 25 voices and speaks, at RTF 0.77, slower
than the ONNX CPU engine); I have not run it on an NVIDIA GPU. The speed figures above are the VieNeu authors', not
mine.

For the English model on CUDA, the `[cuda,cudnn]` extras install CUDA 12 and cuDNN 9 as Python packages, and the worker asks ONNX Runtime to find them
before it starts (`preload_dlls`). If you installed the CUDA toolkit and cuDNN yourself instead, their folders must be
on the `PATH`. With only the ordinary `onnxruntime` installed, `TTS_DEVICE=cuda` stops with "needs
CUDAExecutionProvider ... Install onnxruntime-gpu instead of onnxruntime". The need for `onnxruntime-gpu` was reported
from a Windows PC with an NVIDIA GPU; I did not run these steps on Windows myself, and no GPU speed was measured.

**"cuDNN is unavailable ... LoadLibrary failed for cudnn64_9.dll"** in the worker's log means ONNX Runtime found the GPU
but not cuDNN 9, which every convolution needs (Turbo has them). The worker then loads that model on the CPU and says
"a model failed its first run on CUDAExecutionProvider ... loading it on the CPU instead". Fix it by installing cuDNN 9
for CUDA 12 where the worker can find it: either `python -m pip install "onnxruntime-gpu[cuda,cudnn]"` into the worker's
environment, or install NVIDIA's cuDNN 9 and add its `bin` folder to the `PATH`. Check with
`python -c "import onnxruntime as ort; ort.preload_dlls(); print('ok')"` and `python -m pip list` (look for
`nvidia-cudnn-cu12`). The start-up test includes a convolution for this reason: with the fix missing, `--device cuda`
now stops at start with the same message, and `auto` uses the CPU for every model with a warning, instead of
discovering it on the first chapter. I have not run this on Windows myself.

The flag only changes which hardware the model libraries are told to use; the ONNX Runtime package decides what is
possible, and the ordinary `onnxruntime` package can only use the CPU. The two packages cannot be installed together:
`pip uninstall onnxruntime` first, then `pip install onnxruntime-gpu==1.22.0` (or `onnxruntime-directml`).

**Asking for a device that is not there is an error at start**, with a message naming the package to install, so a
worker never carries on silently on the CPU after you asked for a GPU. `auto` is the forgiving choice.

**The GPU is tried before it is relied on.** At start, a GPU choice is tested on a tiny model (one convolution, so a missing cuDNN shows up) in a separate process,
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
protocol is **4**; the server also understands 1, 2 and 3. Protocol 4 removed `POST /models/{model}/ensure` (the worker
fetches models itself) and added `offered` and `device` to each model in `GET /models`; protocol 3 added `GET /models`;
protocol 2 added `POST /infer` and `capabilities`.
