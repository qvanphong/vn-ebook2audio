# Operations

## Running for the long term

- Start it with `docker compose up -d --build` (the compose file restarts it unless stopped) or as a service running
  `python -m app` with `DATA_DIR` set. The images build from `Dockerfile.server` and `Dockerfile.worker` and were run
  together on a Mac (linux/arm64); an amd64 NAS build has not been tried.
- Put `./data` on a disk with room: audio is about 0.5 MB per minute of speech (AAC 64 kbit/s), so a 10-hour book is
  about 280 MB; models add about 1 to 1.5 GB.
- `/healthz` answers `{"ok": true}` once the server is up; the container health check uses it.
- Logs go to standard output (`docker compose logs -f ebook2audio`): job start and finish, which machine rendered which
  chapter, machines dropping out, model downloads on a worker.

## Backups

Back up `data/library`, `data/books`, `data/config` and `data/voices`. Everything else is a cache or can be rebuilt
(see the table in [Configuration](configuration.md)). Restoring `library` and `books` is enough to get every book back,
because folders are named by the book's MD5 and `book.json` records everything about each file.

To move to another machine, copy `data/` and start the service there; models download again on first use.

## Upgrading

1. Stop the service. Jobs in progress go back to `queued` and resume when it starts again (finished chapters are kept).
2. Pull the new code and, for a native install, `pip install -r requirements.txt -r requirements-engine.txt`.
3. Start it again. An older `engine.json` and an older job database are read and upgraded in place.
4. **Restart workers too** if a release changed the worker protocol. The server still understands older workers (1 and
   2) but only protocol 3 workers can be told to download models.

A symptom of forgetting to restart: the page shows a new section but its calls answer `{"detail": "Not Found"}`. The
page is read from disk on every load; the API code is only read at start.

## When something is wrong

| Symptom | What to check |
|---|---|
| The voice list is empty and says the model is loading | First use downloads the model (several minutes on a slow link). The page retries by itself. Check the log for download errors and that the machine can reach huggingface.co |
| "the voice models are not installed on this server" / a job fails with "needs the … package" | This server lacks `vieneu` or `supertonic`. Install `requirements-engine.txt`, or send speech to a worker |
| A job stops with "cannot reach the voice worker at …" | The worker is off, the address is wrong, or the network dropped for more than about 20 seconds. Start it and press Retry; finished chapters are kept |
| "the worker rejected the token" | The token in Settings differs from the worker's `WORKER_TOKEN` |
| A job stops with "no machine could finish the book (…)" | Every chosen machine failed; the message gives each reason (model not loadable, voice missing, offline) |
| A machine is greyed out in "Run on" | It is offline, cannot run the chosen model (a phone with a non-Nano model), or cannot use the cloned voice you chose |
| Conversion is slower than expected in Docker | On an Apple Mac, Docker Desktop ran the same model about six times slower than native. Set `TTS_THREADS` to the CPUs available, and measure with `python -m app.bench` |
| Out of memory | A model stays loaded once loaded: Nano about 700 MB, Turbo about 1.6 GB. Do not preload more than you use; nothing unloads a model |
| A PDF is refused | It has no text layer (a scan). OCR it first |
| MOBI, AZW3 or FB2 fails | Calibre's `ebook-convert` is not installed (build the image with `WITH_CALIBRE=1`) |
| Chapters are split wrongly | Try another book processor in step 2 and use **Peek**; see [Book processing](book-processing.md) |
| A chapter has no audio but the job is done | It was not selected, or the job ended in an error; the Jobs tab shows the reason |
| Calibre-Web "has no OPDS catalogue" | Enable OPDS in Calibre-Web's feature configuration |

## Security

- **The web page and the API have no login.** Anyone who can reach port 8765 can upload books, delete your library,
  read the machine list (tokens are never returned, but addresses are), browse your Calibre-Web library through the
  saved connection, and start work on your workers. Keep the service on a trusted network, or put it behind a reverse
  proxy that adds authentication and TLS.
- **Workers** accept anyone who has the token (or anyone at all if you set none). Always set `WORKER_TOKEN`.
- **Secrets on disk.** Worker tokens (`engine.json`) and the Calibre-Web password (`calibre.json`) are stored in plain
  text with file mode 600. Treat backups of `data/config` as secret.
- **Traffic.** Server to worker and server to Calibre-Web is plain HTTP unless you use `https://` addresses. Book text
  and tokens cross the network; use a trusted network, a VPN or TLS.
- **Outbound connections.** Only model downloads (Hugging Face), your Calibre-Web address, and your workers. Nothing is
  sent to any other service.
- **Uploads.** Only the extensions the service reads are accepted, files are stored under generated folder names, and
  names sent by the page are matched against known entries or reduced to a bare file name before the disk is touched.

## Licences and use

- VieNeu (Vietnamese models): Apache-2.0 code; check the model cards for the weights.
- Supertonic (English model): code under MIT, weights under **OpenRAIL-M**, which restricts some uses. The upstream
  repository is archived.
- The speech models' output may be subject to their own terms; read them before publishing audio.
- Use books you own, in DRM-free form, for personal use. Only clone voices you have the right to use.
- VieNeu can add an audio watermark only if the optional `resemble-perth` package (with PyTorch) is installed; this
  service does not install it, so audio is not watermarked.

## Known limits

- One job renders at a time.
- A chapter's identity is its title plus text: split a book differently and chapters whose text differs count as new,
  so both sets of files sit in the folder until you delete one.
- One dialogue voice per book, chosen from quotation marks and dashes; it does not know who is speaking.
- Voice cloning for the Vietnamese models only; checked for "not silent", not for likeness.
- Languages: Vietnamese and English. Mixed-language text is read by whichever model the book was given.
- Nothing unloads a model from memory.
- Not verified: running the images on an amd64 NAS, Audiobookshelf scanning of the library folders, Calibre-Web search and paging on a real server, speed on a
  small NAS CPU, a phone as a worker.
