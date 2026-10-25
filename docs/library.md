# The library

The library is the output of the service: plain folders and files that stay useful even if you stop running it.

## Layout

```
data/
  books/<md5>/<original file name>      the book as you gave it, kept once
  books/<md5>/.cover/cover.jpg          a cover chosen for it (for example Calibre-Web's), if any
  library/<md5>/
      0001-<text>-<voice>.m4a           one file per chapter
      0002-<text>-<voice>.m4a
      ...
      cover.jpg  (or cover.png)         the book's cover, if it has one
      book.json                         the manifest (title, author, which file is which)
```

`<md5>` is the 32-character MD5 of the book file's bytes. It is the book's identity everywhere: the folder in
`books/`, the folder in `library/`, and the key the page uses. That holds for every way a book arrives: an upload, the
drop folder, the quick API, or an import from Calibre-Web (the downloaded bytes are hashed as they arrive). The
title is *not* the key; the manifest maps the MD5 to a title, and the page shows the title.

Consequences worth knowing:

- Uploading the same file again finds the same book and its existing audio.
- The same story in another format (an EPUB and an AZW3 of it), or an edited copy of a file, is a different file and
  gets its own folder.
- A title in the book's metadata is used if there is one; otherwise the file name. You can change it in the page.

## Chapter file names

`NNNN-<text>-<voice>.m4a` where

- `NNNN` is the chapter's position in the split of the whole book, plus one, in four digits. It is the position in
  the book, not in your selection: converting only chapter 5 produces `0005-….m4a`.
- `<text>` is the first 8 characters of the chapter's text hash (title plus text).
- `<voice>` is the first 6 characters of the voice-settings hash (model, narrator, dialogue voice, the settings changed from the model's own,
  read titles).

So files for different voice settings sit side by side and never overwrite each other, and re-converting a chapter in
the same voice replaces its file. Sorting by name orders the chapters.

## What is inside a chapter file

- AAC audio, mono, 64 kbit/s, in an MP4 container with the index at the front for quick seeking
  (`-movflags +faststart`). The sample rate is the model's (24 kHz Nano, 48 kHz Turbo, 44.1 kHz English).
- Tags: `title` (the chapter title), `artist` and `album_artist` (the author), `album` (the book title), `track` (the
  chapter number), `genre` = `Audiobook`, `language`.
- The cover as an attached picture, when the book has one. If the book's cover changes later (for example by importing it
  again from Calibre-Web), the picture in existing chapter files is swapped by copying the audio, so nothing is
  re-encoded.

A file is written under a temporary name and moved into place when complete, so a partial chapter never appears in the
library.

## `book.json`

```json
{
  "md5": "0123456789abcdef0123456789abcdef",
  "created": 1792691846.4,
  "updated": 1792725222.9,
  "title": "…", "author": "…", "language": "vi",
  "source_name": "original file name.epub",
  "cover": "cover.jpg",
  "audio": [
    {
      "file": "0003-4e121793-e30098.m4a", "index": 2, "title": "Chương II",
      "text_hash": "4e121793…", "chars": 30300, "seconds": 731.738, "bytes": 5899324,
      "model": "nano", "voice": null, "dialog_voice": null, "params": {"speed": 1.0, "steps": null, "cfg": null, "sway": 0.0, "seed": null, "max_chars": 140}, "read_titles": true,
      "voice_sig": "e30098", "processor": "auto", "created": 1792691900.1
    }
  ]
}
```

Only entries whose file still exists are shown; deleting a `.m4a` by hand simply makes it disappear from the page.
The manifest is named `book.json` so it does not clash with Audiobookshelf's own `metadata.json`.

## Chaptered M4B

In the page, **Download as one chaptered M4B** (or `GET /api/library/<md5>/book.m4b?group=<voice_sig>`) joins one
voice's chapter files into a single M4B, in chapter order, without re-encoding. It has chapter marks named after the
chapters, the book's title, author, language, genre and the cover. It is built the first time and cached in
`data/cache/m4b/` (the cache key includes each file's size, so it rebuilds if a chapter changes).

## Renaming and deleting

- **Rename** (`PATCH /api/library/<md5>`) changes `title` and `author` in `book.json` and rewrites the album, artist
  and album-artist tags of every chapter file by copying the streams, so the audio is not touched.
- **Delete a chapter file** removes the file and its manifest entry.
- **Delete a book** removes `library/<md5>/`. The original in `books/<md5>/` stays.

## Audiobookshelf and other players

Point Audiobookshelf's *Audiobooks* library at `data/library`. Each `<md5>/` folder is one book made of many files
carrying title, author, album and track tags, and a `cover.jpg`. That layout has not been tested with Audiobookshelf
here; the single M4B download is the alternative if you want one file per book. Any player that plays a folder of
`.m4a` files works.

If a book has files in several voices, a folder-based player will see all of them together; the Library tab shows them
as separate groups. Delete the voices you do not want.

## Folders the service also keeps

| Folder | Holds | Cleaned |
|---|---|---|
| `data/uploads/<id>/` | A file you uploaded and have not converted yet, and its Calibre-converted copy | Folders older than 24 hours are removed when the server starts; converting or **Start over** removes it at once |
| `data/cache/samples/` | Voice sample MP3s | Never; safe to delete |
| `data/cache/m4b/` | Built M4B files | Never; safe to delete |
| `data/cache/voices.json` (and `voices-<id>.json`) | Last known voice lists | Safe to delete |
