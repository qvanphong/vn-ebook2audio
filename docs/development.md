# Development

## Layout

```
nas-audiobook/          the service
  app/                  the Python package (see Architecture)
  tests/                the test suite
  tools/                make_golden.py, quantize_nano.py
  golden/               reference data for porting the Nano model
  Dockerfile.server, Dockerfile.worker, docker-compose.yml, docker-compose.worker.yml,
  requirements*.txt, pytest.ini
android-spike/          a phone speed and parity experiment (Kotlin)
docs/                   this documentation
```

## Running the tests

```bash
cd nas-audiobook
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest
```

The suite needs `ffmpeg` and `ffprobe` on the PATH. It uses a fake speech engine, so it needs no models and takes about
20 seconds. At the last run: 278 passed, 3 skipped. Two PDF tests skip without poppler (`pdftotext`); one test that loads
the real English model is off unless you ask for it (it downloads about 390 MB the first time):

```bash
RUN_ENGINE_TESTS=1 pytest tests/test_english_real.py
```

`pytest.ini` puts `.` and `tests` on the path, so tests import `app.*` and `conftest` directly.

### Fixtures and fakes (`tests/conftest.py`)

| Name | What |
|---|---|
| `settings`, `store` | A `Settings` over a temporary data folder and a `JobStore` on it |
| `make_epub(name, chapters, title, author, cover, language)` | Builds a small EPUB (Vietnamese by default) |
| `toc_epub(files, toc, ...)` | Builds an EPUB from `{file: body}` and an explicit table of contents |
| `FakeEngine` | An engine that records the sentences it is given and returns a tone of matching length; `fail_on="text"` makes it raise |
| `FakeBackend`, `FakeHandle` | A model backend that records loads and calls and knows two Vietnamese voices |
| `write_wav` | A short WAV file, for cloning tests |

Tests that exercise workers use FastAPI's `TestClient` as the HTTP client of a `RemoteBackend`, so the real worker and
the real client talk over an in-memory connection. Calibre-Web tests use `httpx.MockTransport` with a fake that
answers OPDS requests.

### Test files

| File | Covers |
|---|---|
| `test_text.py` | Sentences, abbreviations, pauses, dialogue |
| `test_processors.py`, `test_flow.py`, `test_titles.py`, `test_pdf.py`, `test_covers.py` | Splitting methods, the upload and preview flow, titles, PDF, cover finding |
| `test_jobs.py`, `test_library_api.py`, `test_api.py` | Jobs (resume, cancel, overwrite), library routes, general API |
| `test_pool.py`, `test_prepare.py`, `test_models.py` | Model pool, making a machine ready, what a machine has of each model, the passive worker |
| `test_worker.py`, `test_phonemes.py`, `test_golden.py` | The worker protocol, phoneme-only workers, golden reference data |
| `test_engine.py`, `test_balance.py`, `test_balance_api.py` | The machine list, chapters shared across machines |
| `test_english.py`, `test_english_real.py` | The English model, language choice, abbreviations |
| `test_startup.py` | The server and workers start with no model loaded |
| `test_calibre.py` | The Calibre-Web client and its API |

## Adding things

### A chapter-splitting method

Implement the `Processor` protocol from `app/books/base.py` (`id`, `label`, `description`, `params`, `accepts(source)`,
`split(source, params) -> Split`), add it to `PROCESSORS` in `app/books/__init__.py`, and add tests next to
`tests/test_processors.py`. The page needs no change: it lists whatever `accepts` returns and renders `params`
(`text`, `number` or `choice`) as form fields. A section's identity is a hash of its title and text, so nothing else is
needed to work with existing audio.

### A model

1. Add a `ModelInfo` to `CATALOG` in `app/catalog.py` and say how to tell it is downloaded (`downloaded`,
   `bytes_on_disk`).
2. Add it to `MODEL_MODES` and `SAMPLE_RATES` in `app/tts.py`; add it to `NO_CLONE` if it has fixed voices.
3. Write a handle class (see `SupertonicHandle`) with `list_preset_voices`, `voice_ids`, `infer`, `add_voice`,
   `remove_voice` and `can_clone`, and return it from `LocalBackend.load`.
4. Decide when a book should start on it (`Settings.defaults_for`) and add it to the page's model list.
5. Test with a stand-in for the library (see `FakeSupertonic` in `tests/test_english.py`).

The worker, the machine list and the job runner need no change: they work from the catalogue and `MODEL_MODES`.

### Another language

The text pipeline is mostly language-neutral. A new language needs a model (above), a mapping from the book's language
to it in `Settings.defaults_for`, an entry in the abbreviation list in `text.py` if the language has abbreviations that
end with a full stop, and a sample sentence in `samples.py`.

### A worker in another language or runtime

Implement the endpoints in [HTTP API](api.md#voice-worker-protocol-version-3). The Python worker is the reference;
`tests/test_worker.py` and `tests/test_models.py` show the exact exchanges. A worker that cannot do text work declares
`synthesize: false` and receives phonemes.

## Tools

| Tool | Purpose |
|---|---|
| `python -m app.bench --model nano` | Load time, peak memory and speed (RTF) of a model on this machine, with the time a 300,000-character book would take |
| `tools/make_golden.py` | Writes `golden/`: reference data from the Python Nano model, for checking a port. It recomputes the model's loop and refuses to write if the result differs from the library's own output |
| `tools/quantize_nano.py` | Makes the smaller int8 copy of the Nano graphs and measures how far it moves the audio (log-mel distance against the variation between two normal renders) |

## Conventions in the code

- Files that other processes may read are written to a temporary name and renamed (chapters, manifests, caches,
  settings files), so a crash never leaves a half-written file.
- Settings files that hold secrets (`engine.json`, `calibre.json`) are written with mode 600.
- A machine problem is a `LaneError` (or a subclass); anything else is a bug and stops the job.
- Comments say why, not what. The tests describe behaviour a user could see.
- The page is one file by design; keep it dependency-free.

## The Android experiment

`android-spike/` is a Gradle project (AGP 8.13, Kotlin 2.1, ONNX Runtime 1.22) with an Android app and a desktop JVM
harness that run the same Kotlin port of the Nano loop. See [Phone worker](phone-worker.md) for what it showed and the
folder's own `README.md` for how to run it (`gradle :app:assembleDebug`, then `./run-on-phone.sh <models> [threads]
[runs]`, with the phone paired over wireless debugging).
