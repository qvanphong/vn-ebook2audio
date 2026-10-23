# ebook2audio documentation

ebook2audio is a self-hosted service that turns ebooks into audiobooks. You give it an EPUB, PDF, MOBI, text file or a
book from your Calibre-Web library; it splits the book into chapters, reads each one aloud with a local text-to-speech
model, and stores one audio file per chapter in a library that Audiobookshelf (or any player that reads folders of
audio files) can play. Everything runs on your own hardware. Vietnamese and English books are supported.

The speech can be made on the machine that runs the service, on other computers you add as **voice workers**, or on
both at once, sharing each book between them.

## Where to start

| If you want to... | Read |
|---|---|
| Understand what the pieces are and how a book becomes audio | [Overview](overview.md) |
| Install it and convert a first book | [Getting started](getting-started.md) |
| Learn the web page tab by tab | [User guide](user-guide.md) |
| Know how books are split into chapters and how the text is prepared | [Book processing](book-processing.md) |
| Choose models and voices, use English, clone a voice | [Models and voices](voices-and-models.md) |
| Understand the output folders, file names and tags | [The library](library.md) |
| Add more machines to share the work | [Voice workers](workers.md) |
| Browse and convert books from Calibre-Web | [Calibre-Web](calibre-web.md) |
| Look up an environment variable or a file the service keeps | [Configuration](configuration.md) |
| Script the service or build a worker | [HTTP API](api.md) |
| See how the code is organised and how jobs run | [Architecture](architecture.md) |
| Run the tests, add a processor, model or engine | [Development](development.md) |
| Run it for the long term, back it up, fix a problem | [Operations](operations.md) |
| Know what the phone experiment showed | [Phone worker](phone-worker.md) |

## Status in one page

Built and tested:

- Upload, preview, chapter selection and conversion in a web page; a drop folder; a quick API for scripts.
- Six chapter-splitting methods, tried on a real EPUB, with a preview before anything is converted.
- Two Vietnamese models (Nano, Turbo), an English model (Supertonic), voice samples, dialogue voice, voice cloning
  for the Vietnamese models.
- A library keyed by the MD5 of each book file, with a title, author, cover and per-chapter files, and one-click
  chaptered M4B download.
- Several voice workers at once, chapters shared between machines, models downloaded on demand by workers.
- Calibre-Web browsing and import over OPDS.

Not done, or not verified (details in the pages above):

- Both Docker images were built and run together on one Mac (linux/arm64); they have not been run on a NAS (amd64).
- Audiobookshelf scanning of the library folders was not tested.
- The Calibre-Web integration was tested against a stand-in server and, for browsing, covers and import, against one real Calibre-Web instance; search and paging on a real instance were not tried.
- No Android worker app exists; a speed and accuracy experiment on a Pixel 8 Pro showed it is feasible for the Nano
  model.
- Speed was measured on an Apple M1 Pro and a Pixel 8 Pro, not on a small NAS CPU such as an Intel N100.
- I have not listened to the audio from the int8 phone model, the English model, or output made on a second machine;
  English was checked with a speech recogniser, which measures clarity, not how it sounds.
