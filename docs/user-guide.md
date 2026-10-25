# User guide

The page has five tabs: **Convert**, **Calibre**, **Library**, **Jobs** and **Settings**. Your place in a conversion
is kept while you move between tabs.

## Convert

Convert is three steps on one page, with a bar at the bottom showing how many chapters are selected.

### 1. Upload a book

Choose a file. Supported: EPUB, PDF (with a text layer), TXT, Markdown, and through Calibre also MOBI, AZW, AZW3,
FB2, FBZ, DOCX, RTF, ODT, LIT, HTMLZ and KFX. Scanned PDFs need OCR first and are refused with a message. The page
then shows:

- the book's cover (an EPUB's own cover; a PDF's first page);
- a note if the book is already in your library, with how many audio files it has;
- the title and author it found.

### 2. Split into chapters

- **Book processor.** A list of the splitting methods that can read this file. *Automatic* tries the others in turn.
  Each method may have settings (for example the heading levels, a pattern, or the part size); changing one refreshes
  the preview. See [Book processing](book-processing.md).
- **Book title** and **Author.** Prefilled; edit them to change what the library shows and what is written in the
  audio tags. A title you set once is kept if you upload the book again.
- **The chapter list.** For every chapter: its number, title, size in characters and an estimate of the audio length.
  **Peek** shows the first and last words of the chapter, which is the quickest way to see whether a split is right.
  Short chapters carry a *short* badge.
- **Selection buttons.** *All*, *None*, *Invert*, *Recommended* (what the method picks: at least 200 characters, and
  not a contents or copyright page) and, once the book already has some audio, *Without audio*.
- **Audio badges.** A chapter shows *has audio* if audio already exists in exactly the voice settings now chosen, and
  *audio in another voice* if it only exists in other settings. Changing the voice below refreshes the badges.

### 3. Choose a voice

- **Presets.** A saved set of engine (model), narrator, dialogue voice and every voice setting of that model. Pick one to apply it. To keep
  the settings on the form as a preset, type a name and press **Save preset**; with a preset picked and no name typed,
  the button updates that preset. **Delete** removes the picked one. Presets are kept on the server, so every browser
  sees them.
- **Model.** English (Supertonic), Vietnamese Nano or Vietnamese Turbo. When you upload an English book the page
  switches to English and its voices and says so; a Vietnamese book switches back to your Vietnamese default.
- **Voice settings.** Shown under the model, and different for each: Nano has speed, quality steps, guidance, sway, seed
  and the longest piece; Turbo has speed (applied to the finished audio, so extremes sound less natural), expressiveness (temperature), top K, top P, the repetition penalty and window, the
  longest piece in frames and in characters; English has speed, quality steps, the pause between pieces and the longest
  piece. An empty box uses the model's own value. Each box says what it does.
- **Narrator** and **Dialogue (quoted speech).** The narrator reads everything; if you choose a dialogue voice, text
  inside quotation marks (three words or more) and lines that start with a dash are read by it.
- **Read each chapter title aloud.** On by default.
- **Run on.** Appears when you have more than one machine. Tick the machines that should share this book. Each line
  says what that machine will have to do ("English model ready", "has the model on disk and will load it first",
  "will download it (about 390 MB) first, then start") or why it cannot take part (offline, cannot run this model,
  cannot use a cloned voice). See [Voice workers](workers.md).
- **Voice gallery.** Every voice of the chosen model as a card with **Hear**, **Use as narrator** and **Use for
  dialogue**. Type your own sample text (up to 300 characters) to hear a voice read something you care about. Samples
  are cached, so a second listen is instant.
- **Clone a voice** (at the bottom of the tab). A clean 3 to 15 second clip of one speaker, any format ffmpeg reads.
  It becomes a voice in the Vietnamese models. Only clone voices you have the right to use. The English model cannot
  clone.

### Confirm and convert

The bottom bar shows `N chapters · about X min`. **Convert** opens a summary: book, number of chapters and estimated
length, splitting method, voice, machines, and where the files will be saved. It also warns you:

- *N selected chapters already have audio in this voice.* Converting again replaces them. You can **Skip the N with
  audio**, or **Convert all (replace)**.
- *N have audio in another voice.* The new audio is added next to it; nothing is replaced. Such a chapter shows two
  small buttons: **ℹ️** opens what that audio was made with (engine, narrator, dialogue voice, every voice setting, whether
  titles were read, when it was made, its length and file), and **✍️** sets the engine, voices and voice settings on
  the form to match the newest of that audio, so the chapter converts in the same voice.

**Start over** discards the uploaded file and returns to step 1.

## Calibre

Browse your Calibre-Web library and send a book into Convert without downloading and re-uploading it. Needs a
connection saved in Settings. Each book has **Convert** buttons (one per format this app reads) and, under them,
**Preview**, which opens the book's page in Calibre-Web in a new tab. See [Calibre-Web](calibre-web.md).

## Library

Every book you have converted, newest activity first. A card shows the cover, title, author, language, number of
audio files, total length, the voice settings used (as chips), the book's MD5 and its original file name.

- **Open** shows the book's audio grouped by voice settings. Each group has a player that moves on to the next
  chapter by itself, a chapter list (click to play, `×` to delete one file), and **Download as one chaptered M4B**.
  The M4B is joined from the chapter files without re-encoding, with chapter marks and the cover, and cached.
- **Rename** changes the title and author in the library and in the tags of every chapter file. The folder keeps its
  MD5 name. A renamed book keeps its name when you upload it again.
- **Delete this book and all its audio** removes the library folder. The original book file in `data/books/` is kept.

## Jobs

One row per conversion, newest first. A row is a single line: `Job #id · book · chapters`, the engine and voice
it uses, a progress bar with the percentage, and on the right an icon for its state:

- ✅ **Done** (green)
- ⏳ **Working** (blue): queued, waiting for a machine, or running
- ❌ **Failed or cancelled** (red)
- ⏸️ **Paused** (amber)

Press a row to open it. Inside you find:

- **Pause** (a queued job waits; a running one stops after the sentence in progress and keeps its finished chapters),
  **Resume** (for a paused job), **Cancel**, **Retry** (for a failed or cancelled job; finished chapters are reused)
  and **Remove from list**;
- **Open in Library** once the job is done;
- one line per machine while it runs: what it is doing ("loading the model", "downloading … (182 of about 280 MB)",
  "sending the cloned voice", or the chapter and clip it is on), or why it dropped out;
- every chapter with its own icon: ✅ made (playable at once), ⏳ being made, ○ waiting, ⏸️ paused, ❌ not finished;
- the reason when the job failed.

**Clear** at the top removes from the list the succeeded, the failed or the paused jobs. It never removes audio, and it
never touches a job that is still running or queued. The red badge on the Jobs tab counts active jobs.

## Settings

- **Voice machines.** This server and any number of voice workers: add, test, switch on or off, remove, and see what
  models each one has, listed in two columns (one card per model: its state, and a button to download or load it
  ahead of time). Each machine also has **Texts per request** (1 to 32, default 1): how many sentences it is sent in one
  request. A GPU works on them together; a CPU runs them one after another, so there a larger number only saves round
  trips. See [Voice workers](workers.md).
- **Calibre-Web.** Address, username and password of your Calibre-Web server, with Test, Save and Disconnect.
- **How to start a worker.** The commands for a worker machine.

## Pronunciations and pauses

- **Pronunciations.** Put `from=to` lines in `data/config/pronunciations.txt` (whole words, case-insensitive,
  longest match first, `#` for comments). Example: `iPhone 15=ai phôn mười lăm`. It applies to every book and is read
  when a job starts.
- **Explicit pauses.** Write `[pause 1.5]` or `[nghỉ 2s]` in the text for a pause of 0.1 to 10 seconds. This only
  helps with plain text files you edit yourself.
