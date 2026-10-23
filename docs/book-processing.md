# Book processing

From a file to the sentences that are spoken: loading, splitting into chapters, and preparing the text.

## Loading

| Format | How it is read |
|---|---|
| EPUB | Parsed as a structured book (table of contents, reading order, cover, metadata) |
| TXT, Markdown (`.txt .md .markdown`) | Read as UTF-8 text. Title is the file name; author is "Unknown" |
| PDF | `pdftotext` (poppler), words broken by a hyphen at a line end are rejoined. A PDF with almost no text is refused ("needs OCR first") |
| MOBI, AZW, AZW3, FB2, FBZ, DOCX, RTF, ODT, LIT, HTMLZ, KFX | Converted to EPUB with Calibre's `ebook-convert`, then read as an EPUB. The converted copy is kept next to the upload so previews are quick. Without Calibre installed the job ends in `error` saying so |

**EPUB text extraction.** Only the body of each document in reading order is used, so a `<title>` is never spoken.
Scripts, styles, `nav`, and footnote asides and note references are dropped. Block elements (paragraphs, headings,
list items, table rows, quotes and so on) become line breaks, so a paragraph is one line.

**Language.** Taken from the book's language tag; if the tag is missing or says `en` but more than 2% of the letters
are Vietnamese-specific, the book counts as Vietnamese; with no tag and no such letters it counts as English. The
language decides which model a book starts with (see [Models and voices](voices-and-models.md)).

**Cover.** An EPUB's declared cover is used, in this order: an item marked `cover-image` (EPUB 3), the item named by
`<meta name="cover">` (EPUB 2), the cover page in the `<guide>` (and the picture inside it, including the SVG
wrapper some tools generate), and only then an image whose name contains "cover". The bytes decide the type (JPEG or
PNG), not the file name. A book with pictures but no cover gets none rather than a random picture. A PDF's cover is its
first page, rendered by `pdftoppm`.

A cover can also be **chosen** for a book file: a picture kept beside it in a `.cover` folder wins over anything inside
the file. Importing from Calibre-Web does this with Calibre-Web's own cover (see [Calibre-Web](calibre-web.md)), because
Calibre stores covers separately from the EPUB and the two often differ. The chosen cover stays with the book in
`data/books/<md5>/.cover/`.

## Book processors

A processor turns the loaded book into an ordered list of chapters (`Section`: title and text). The page lists only
the processors that can read the uploaded file and shows the result as a preview, with each chapter's size and its
first and last words, before anything is converted.

| Processor (`id`) | What it does | Settings |
|---|---|---|
| **Automatic** (`auto`) | Tries the next five in this order and keeps the first that finds at least two real chapters: table of contents, headings, one chapter per file, title lines, equal parts. Says which it chose | none |
| **EPUB table of contents** (`epub-toc`) | Uses the book's table of contents for titles and boundaries, including entries that point inside a file (anchors). Best for well-made EPUBs | none |
| **One chapter per file** (`epub-spine`) | Each content file, in reading order, is a chapter, titled by its first heading or the table of contents | none |
| **Split at headings** (`epub-headings`) | A new chapter at every heading tag, across files. For books with headings but a missing or wrong table of contents | Heading levels: `h1`, `h1-h2` (default), `h1-h3` |
| **Chapter title lines (pattern)** (`text-pattern`) | Reads the whole book as text and starts a chapter at every line that matches a regular expression. Works for any format | Pattern (case-insensitive). The default matches lines such as `Chương 3`, `Chapter IV`, `Part 2`, `Book 1`, `Section 5`, and Markdown `#` headings |
| **Equal-sized parts** (`fixed-size`) | Cuts the text into parts of about the same length at paragraph boundaries, named Part 1, Part 2 | Characters per part, default 15,000 (about 17 minutes of audio) |

All the EPUB methods remove a line near the top of a chapter that repeats the chapter's title (and up to three lines
above it, such as a running header), because the title is read separately.

**Which chapters are selected by default.** Those with at least 200 characters whose title is not a contents or
copyright page (`Mục lục`, `Table of contents`, `Contents`, `Copyright`, `Bản quyền`). You can change the selection
freely.

**A chapter's identity.** `text_hash` is the first 16 hex characters of SHA-1 over the title, a newline and the chapter
text. It is how the page knows a chapter already has audio. Split the same book differently and chapters whose text is
identical keep matching; chapters whose text differs are new.

## Preparing the text

Done per chapter when a job runs (`text.py`).

1. **Title.** If *read titles* is on, the chapter title is the first clip, followed by a long pause.
2. **Paragraphs.** Each line is a paragraph. If dialogue is on (a dialogue voice is set), a paragraph is first cut
   into narrated and quoted parts.
3. **Cleaning.** Web addresses are removed; footnote marks (`[12]`, `(3)` after a word, superscript digits) are
   removed; bullets (`•`, `*`) become commas; a slash between words becomes a comma; whitespace is collapsed.
4. **Pronunciations.** Your `pronunciations.txt` replacements are applied.
5. **Sentences.** Text is split at `. ! ? …` (with any closing quote or bracket). It does not split after an
   abbreviation: `Dr. Mr. Mrs. Ms. Prof. Gen. Col. Capt. Lt. Sgt. Gov. Rev. Hon. Mt. Ft. Cf. St. Jr. Sr. Inc. Ltd.
   etc. vs.` and the Vietnamese `TP. TT. TS. ThS. PGS. GS. BS. KS. NXB. Tr.`, a single capital initial (`J. K.`),
   dotted abbreviations (`e.g.`, `i.e.`, `a.m.`, `p.m.`, `U.S.`, `Ph.D.`), or a lone number or letter.
6. **Long sentences.** A sentence over 260 characters is split at a comma, semicolon, colon or dash that leaves at
   least 120 characters before it and 40 after; anything still over 2,500 characters is cut at a space. (The models
   also chunk text themselves: Nano works in pieces of at most 140 characters.)
7. **Short fragments.** A fragment of two words or fewer is merged into the next sentence if the result stays within
   300 characters.
8. **Pauses.** Each clip is followed by silence chosen from how it ends, minus a 120 ms lead trim (minimum 40 ms):

   | Ending | Pause after (ms, before trim) |
   |---|---|
   | `.` | 480 |
   | `?` or `!` | 540 |
   | `…` or `...` | 600 |
   | `:` | 380 |
   | `,` or `;` | 300 |
   | anything else | 360 |
   | end of a paragraph | 720 |
   | after the chapter title | 1,300 |
   | end of the chapter | 1,000 |

   `[pause 1.5]` and `[nghỉ 2s]` in the text override the pause with an explicit one of 0.1 to 10 seconds.
9. **Dialogue.** A quoted span of three words or more, in `“…”`, `«…»` or `"…"`, is dialogue; so is a line that
   starts with a dash (`-`, `–`, `—`). Everything else is narration. There is one dialogue voice per book: the page
   cannot tell who is speaking.

The model then turns each sentence into audio. Numbers, dates and similar are spoken by the model's own text
normaliser, not by this service.
