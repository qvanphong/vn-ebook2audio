# Overview

## What it does

```
 book file ──► split into chapters ──► prepare text ──► speak each chapter ──► encode ──► library
 (EPUB, PDF,     (6 methods,           (normalise,       (a model, on this       (AAC .m4a    data/library/<md5>/
  MOBI, TXT,      you preview and       sentences,        machine or on           per chapter,  0001-….m4a, cover, book.json
  Calibre-Web)    choose chapters)      pauses,           workers)                tags, cover)
                                        dialogue)
```

The web page walks you through it: **upload → process and preview → select chapters → confirm → convert**. A job then
renders the chapters you picked. Each chapter becomes an `.m4a` file the moment it is done, so you can play finished
chapters while the rest is still being made, and a stopped job loses at most the chapter it was working on.

## The ideas to know

**Book.** One book file. It is identified by the **MD5 of its bytes**, never by its title. The same file always lands
in the same library folder; a different file (another format, an edited copy) is a different book.

**Chapter.** One section of a book that becomes one audio file. A chapter's identity is a hash of its title and text
(`text_hash`), so the service can tell that a chapter already has audio even if you split the book differently later.

**Processor.** A way of splitting a book into chapters (table of contents, headings, one per file, title lines,
equal parts, or automatic). You pick one in the page and see the result before converting.

**Model.** A text-to-speech model: Vietnamese Nano, Vietnamese Turbo, or English (Supertonic). A model has voices.

**Voice.** A named speaker of a model. A conversion has a narrator voice and, optionally, a second voice for quoted
speech. Cloned voices (from a short recording) work with the Vietnamese models.

**Voice settings.** The model, narrator, dialogue voice, expressiveness and "read titles" choice together. Different
settings never overwrite each other: their files sit side by side in the book's folder, told apart by a 6-character
signature in the file name.

**Machine.** Somewhere speech can be made: this server itself, or a **voice worker** (any other computer running
`python -m app.worker`). You keep a list of machines and choose which ones share each book.

**Job.** One conversion request: a book, a chapter selection, voice settings and the machines to use. Several jobs
can run at once, each on machines the others are not using; within a job, chapters are shared between the machines.

**Library.** `data/library/<md5>/`, one folder per book holding the chapter files, a cover and a manifest
(`book.json`).

## The parts

| Part | What it is | Where |
|---|---|---|
| Web page | One HTML file, no build step. Tabs: Convert, Calibre, Library, Jobs, Settings | `nas-audiobook/app/static/index.html` |
| Server | A FastAPI app: uploads, previews, jobs, library, machine list, Calibre-Web | `nas-audiobook/app/api.py` and neighbours |
| Job runner | A thread that takes queued jobs and renders their chapters | `jobs.py` |
| Book processors | Split a loaded book into chapters | `app/books/` |
| Text pipeline | Cleans text, splits sentences, decides pauses and dialogue | `text.py` |
| Model pool | Loads models once, keeps cloned voices enrolled, serialises use of each model | `tts.py` |
| Catalogue | The models this server knows and how to tell whether a machine has one | `catalog.py` |
| Machine list | Which machines exist, which are on, live status | `engine_config.py`, `fleet.py` |
| Voice worker | The same models behind a small HTTP API, on another computer | `worker.py`, `remote.py` |
| Library | Folders, manifests, covers, renaming, M4B building | `library.py`, `m4b.py` |
| Calibre-Web client | Browses the OPDS catalogue and downloads books | `calibre.py` |
| Drop folder watcher | Queues files put in `data/inbox` | `watcher.py` |

Outside the service: `android-spike/` (a phone speed experiment), `nas-audiobook/golden/` and
`nas-audiobook/tools/` (reference data and tools for porting the Nano model), the reference folder at the project root (research notes and the first,
single-file version of the converter, kept for history).

## Two places the work can be shared

1. **Between chapters of one book, across machines.** Each machine in a job takes the next free chapter. A faster
   machine does more. This is how a laptop and a NAS (or a phone) cooperate. See [Voice workers](workers.md).
2. **Between jobs, by machine.** A machine works on one book at a time (a model already uses all the CPU threads
   it is given), but a machine that has nothing left to do for one book moves on to the next queued book while the
   others finish the first. See [How a book is shared](workers.md#how-a-book-is-shared).

## What it never does

- It never sends your books or audio anywhere outside your network. The only outside traffic is downloading models
  (from Hugging Face) the first time one is used, and talking to a Calibre-Web or worker address you entered.
- It does not remove DRM. Use books you own in DRM-free form, for personal use.
- It does not know who is speaking in a story. There is one narrator voice and one dialogue voice per book, chosen by
  quotation marks and dashes.
