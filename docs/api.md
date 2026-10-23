# HTTP API

Everything the page does goes through this API, so anything the page can do you can script. Base URL
`http://HOST:8765`. Bodies are JSON unless noted. Errors are `{"detail": "message"}` with a status code: 400/422 for a
bad request, 404 for something that does not exist (or an upload that expired), 409 for a state conflict, 415 for an
unsupported file type, 502 when a voice worker or Calibre-Web refused or could not be reached, and 503 (with
`{"detail": {"loading": bool, "message": str}}`) while a model is loading and no saved voice list exists yet.

**There is no authentication on this API or the page.** Keep it on a network you trust. See [Operations](operations.md).

## Quick conversion for scripts

`POST /api/jobs` (multipart: `file`, optional `model`, `voice`, `dialog_voice`, `temperature`, `read_titles`) queues a
book with automatic chapter detection and every readable chapter, and returns the job. Chapters that already have audio
in the same voice settings are skipped. Without `model` the book's language chooses it (English starts on the English
model). 415 for an unsupported extension.

```bash
curl -F file=@book.epub -F model=nano http://NAS:8765/api/jobs
```

## Convert flow

| Call | Meaning |
|---|---|
| `POST /api/uploads` (multipart `file`) | Step 1. 201 `{id, filename, md5}`. The file is kept under `data/uploads/<id>` for 24 hours |
| `GET /api/uploads/{id}` | `{id, filename, md5, kind, title, author, language, has_cover, in_library, audio_count, processors: [...]}`. `kind` is `epub` or `text`. Each processor is `{id, label, description, params: [{name, label, kind, default, choices, help}]}`; only processors that can read the file are listed |
| `GET /api/uploads/{id}/cover` | The book's cover image; 404 if it has none |
| `POST /api/uploads/{id}/preview` `{processor, params}` | Step 2. `{md5, title, author, language, processor, params, note, audio_count, chapters: [...]}`. Each chapter: `{index, title, chars, seconds, head, tail, selected, text_hash, audio: [...]}` where `audio` lists existing files for that chapter (voice, model, file name, `voice_label`). `processor` defaults to `auto`; 422 for a bad setting or an unreadable book |
| `POST /api/uploads/{id}/convert` | Step 3. Body `{processor, params, selection, overwrite, title, author, model, voice, dialog_voice, temperature, read_titles, workers}`. `selection` is a list of chapter indexes (omitted = the recommended ones); `overwrite` replaces existing audio in the same voice settings (otherwise those chapters are skipped); `workers` is a list of machine ids from `/api/workers` (omitted = every machine that is on; unknown ids are dropped; none left = 422). Returns 201 with the job plus `already_had_audio`. The stored upload is moved into `data/books/<md5>/` |
| `DELETE /api/uploads/{id}` | Discard an upload |
| `GET /api/processors` | Every processor with its settings |

Model, voice, dialogue voice, expressiveness and title reading default to the environment settings and the book's
language (see [Models and voices](voices-and-models.md)). A default voice never carries over to a model it does not
belong to.

## Jobs

| Call | Meaning |
|---|---|
| `GET /api/jobs`, `GET /api/jobs/{id}` | Jobs, newest first |
| `POST /api/jobs/{id}/cancel` | A queued job is cancelled at once; a running one stops after the sentence in progress |
| `POST /api/jobs/{id}/retry` | An `error` or `cancelled` job goes back to `queued`; finished chapters are reused |
| `DELETE /api/jobs/{id}` | Remove from the list (409 while active) |

A job:

```json
{
  "id": "494c497c284c", "title": "…", "state": "running",
  "model": "english", "voice": "M2", "dialog_voice": "F2", "temperature": 0.8, "read_titles": true,
  "chapters_done": 1, "chapters_total": 2, "output": ".../library/<md5>", "error": null, "book_md5": "…",
  "created": 1792740000.0, "updated": 1792740030.0,
  "processor": "auto", "selected": 2,
  "chapters": [{"title": "Chapter 1", "seconds": 69.9, "file": "0001-ebb5dd6d-4bd713.m4a"}, {"title": "Chapter 2", "seconds": null, "file": null}],
  "workers": [{"id": "local", "label": "This server", "state": "rendering", "detail": "",
               "chapter": "Chapter 2", "clip_index": 4, "clip_count": 10, "done": 1, "error": null}]
}
```

`state` is one of `queued`, `running`, `assembling` (reserved), `done`, `error`, `cancelled`. Each entry of `workers`
has a `state` of `waiting`, `preparing` (with `detail` saying what: loading, downloading, sending a cloned voice),
`rendering`, `idle`, `done` or `failed` (with `error`). `selected` is the number of chapters chosen, or `null`.

## Library

| Call | Meaning |
|---|---|
| `GET /api/library` | Every book: `{md5, title, author, language, source_name, has_cover, audio_count, seconds, voices, updated}` |
| `GET /api/library/{md5}` | The manifest without its file list, plus `groups`: one per voice settings, `{voice_sig, label, model, voice, dialog_voice, temperature, seconds, bytes, files: [...]}` |
| `PATCH /api/library/{md5}` `{title, author}` | Rename; also rewrites the tags of every chapter file without re-encoding |
| `GET /api/library/{md5}/cover` | The cover image |
| `GET /api/library/{md5}/audio/{file}` (`?download=true` to save) | A chapter file, with range requests so players can seek. Playable while the job still runs |
| `GET /api/library/{md5}/book.m4b?group=<voice_sig>` | One chaptered M4B joined from that voice's files |
| `DELETE /api/library/{md5}/audio/{file}` | Remove one chapter file |
| `DELETE /api/library/{md5}` | Remove the book's audio (the original book file is kept) |

## Models and voices

| Call | Meaning |
|---|---|
| `GET /api/models` | The catalogue: `[{id, label, language, description, size_mb}]` |
| `GET /api/voices?model=` | `{voices: [{id, label, cloned}], ready, error}` from the first machine that is on. `ready: false` means the list came from the saved copy and the model is still loading. Asking starts loading the model if it is not loaded. 503 only when nothing has been saved yet |
| `GET /api/voice-sample?model=&voice=&text=&temperature=` | A short MP3 of a voice, cached on disk. `text` up to 300 characters |
| `POST /api/voices` (multipart `name`, `file`) | Clone a voice from a 3–15 s clip. 201 `{id, cloned: true}`. 400 for a bad clip or a built-in name |
| `DELETE /api/voices/{name}` | Remove a cloned voice here and on every worker that can be reached |

## Machines

| Call | Meaning |
|---|---|
| `GET /api/engine` | The saved list: `{local: {available, enabled, models}, workers: [{id, name, url, has_token, enabled}]}`. Tokens are never returned |
| `PUT /api/engine` | `{local_enabled, workers: [{id?, name, url, token?, clear_token?, enabled}]}`. A worker without `id` is new; a blank `token` keeps the saved one; `clear_token` removes it. 422 for a bad address, a duplicate, `local_enabled` without the packages, or nothing switched on |
| `POST /api/engine/test` | `{worker_url, worker_token?, worker_id?}` → `{ok, latency_ms, name, models, states, capabilities}` or `{ok: false, error}` |
| `GET /api/workers` | Every machine live: `{id, name, kind, url?, enabled, online, latency_ms?, error, models, capabilities}`. `device` is `{requested, providers, active, using_gpu, fallback}` (what the machine runs its models on; `null` for an old worker). `models` maps each model to `{ready, state, downloaded, supported, bytes, size_mb, error}` (or just `{ready}` for an old worker). Worker answers are reused for 4 seconds |
| `POST /api/workers/{id}/models/{model}` | Ask a machine to download and load a model now (202). 404 for an unknown machine or model |

## Calibre-Web

| Call | Meaning |
|---|---|
| `GET /api/calibre` | `{configured, url, username, has_password}` |
| `PUT /api/calibre` | `{url, username, password?, clear_password?}`. An empty `url` disconnects. 422 for a bad address |
| `POST /api/calibre/test` | Same body → `{ok, title, sections, books_on_first_page}`; 502 with the reason on failure |
| `GET /api/calibre/feed?path=&q=` | One catalogue page: `{title, entries, next, search, total}`. An entry is a `book` (`id, title, authors, summary, cover` (thumbnail), `image` (full size), `web_url` (the book's page in Calibre-Web, or `null`), `formats: [{format, path, size}]`, `unsupported`) or a `nav` section (`title, path, summary`). `q` searches. 409 if not connected |
| `GET /api/calibre/image?path=` | A cover, fetched through this server |
| `POST /api/calibre/import` `{path, title, format, cover?}` | Download a book into an upload. With `cover` (the entry's `image` path) Calibre-Web's own cover is fetched and used instead of the one inside the file; if that book is already in the library its cover and its chapter files' pictures are replaced. Returns `{id, filename, md5, cover, library_cover_updated}` where `cover` is `calibre-web` or `file` |

## Other

`GET /healthz` → `{"ok": true}` (the Docker health check); `GET /` serves the page; `GET /api/defaults` →
`{model, voice, dialog_voice, english, temperature, read_titles, formats, min_chapter_chars, chars_per_second}`
(`english` is what an English book starts with, or `null`).

## Voice worker protocol (version 4)

Anything that implements this is a worker, including a future phone app. If the worker has a token, every request
needs `Authorization: Bearer <token>` (401 otherwise). The server also understands protocols 1 and 2.

| Call | Meaning |
|---|---|
| `GET /health` | `{ok, protocol: 4, name, device, models: {<id>: {ready}}, capabilities: {synthesize, infer: ["nano"], clone}}`. `synthesize: false` means the worker takes phonemes only (use `/infer`) |
| `GET /models` | `{models: {<id>: {state, ready, loading, downloaded, offered, supported, device, bytes, size_mb, error}}}`. `state`: `ready` (in memory), `loading`, `downloading` (`bytes` arrived so far), `downloaded` (on disk, not loaded), `absent`, `error`. `offered: false` means the worker chose not to serve it (`/voices`, `/synthesize` and `/infer` answer 501); `supported: false` means it could never run it; `device` is `{device, fallback, note}`, where the worker runs the model (`cpu`, `cuda`, ...), its own decision |
| `GET /voices?model=` | `{voices: [{id, label, cloned}], ready, error}`; 503 `{"detail": {"loading": true}}` while the model loads (asking also starts the load) |
| `POST /synthesize` `{model, text, voice, temperature}` | Text in (up to 6,000 characters). Out: mono 16-bit little-endian PCM, sample rate in the `X-Sample-Rate` header (24000 Nano, 48000 Turbo, 44100 English). 501 if `synthesize` is false |
| `POST /infer` `{model, phonemes, voice, steps, cfg, speed, seed}` | One already-phonemised chunk in, one chunk of audio out (PCM as above, trimmed and faded). Nano only; the worker does no text work |
| `POST /clones` (multipart `name`, `file`), `DELETE /clones/{name}` | Add or remove a cloned voice. 501 if `clone` is false |

The server's side of the protocol: a request is retried on network errors and on 503 up to four times with growing
pauses (about 20 seconds) before the machine is dropped from a job; before a machine's first chapter the server asks it
for the job's model (asks for its voices, which starts a download on a worker that serves it, then polls `/models`) and
checks the job's voices. The server never tells a worker to download or which device to use.
