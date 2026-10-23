# Calibre-Web

The **Calibre** tab browses a Calibre-Web library and sends a book into Convert without downloading and re-uploading
it by hand.

## Connect

1. In Calibre-Web, switch on OPDS: **Admin → Edit Basic Configuration → Feature Configuration → Enable OPDS**.
2. In ebook2audio, open **Settings → Calibre-Web** and enter the address (for example `http://192.168.1.10:8083`),
   username and password.
3. Press **Test**. It reports the catalogue's name and its sections, or says what is wrong.
4. Press **Save**.

The address, username and password are stored in `data/config/calibre.json` (file mode 600). The password is never sent
back to the page: a blank password field keeps the saved one, and **Disconnect** clears the saved address, username
and password. Using the same address with a different username drops the saved password.

## Use

- The tab opens on **Recently added**. The **Library** link in the trail at the top goes to the catalogue's sections
  (authors, series, categories and whatever else your Calibre-Web offers); click a section to open it.
- **Search** asks Calibre-Web, so it searches whatever Calibre-Web searches (title, author, series, tags).
- **Load more** continues a long list.
- A book shows its cover, authors and the start of its description, and a button for each format this app can read,
  best first (EPUB, then AZW3, MOBI, FB2, DOCX, PDF, TXT). A format it cannot read (CBZ, for example) is named but has no
  button.
- **Preview**, under the Convert button, opens the book's own page in Calibre-Web in a new browser tab, so you can check
  the description, cover and formats before converting. It uses the address saved in Settings, so that address has to be
  reachable from your browser as well as from this server (an address that only works inside Docker will not open), and
  you need to be signed in to Calibre-Web in that browser, or it shows its login page first and then the book.
- **Convert EPUB** (or another format) downloads the book to this server and opens it at step 2 of Convert, as if you
  had uploaded it. EPUB splits into chapters best, so it is the first choice.

**The cover is Calibre-Web's, not the file's.** Calibre keeps each book's cover in its own library, and that picture can
differ from the one inside the EPUB (you replaced it, or Calibre fetched a better one, or the EPUB's own cover is only a
title page). So an import also downloads the cover Calibre-Web shows and uses it instead of the one inside the file:
it is what the upload preview shows, what the library shows, and what is written into the chapter files. If the cover
cannot be fetched, the file's own is used. If you import a book that is **already in your library**, its library cover
and the picture in each of its chapter files are replaced right away (the audio is copied, not re-encoded).

The book is identified by the MD5 of the downloaded bytes, like any other upload, so the library recognises it if you
have converted that exact file before. Calibre's own book id is not used. See [The library](library.md).

## How it works

Calibre-Web serves an OPDS catalogue (an Atom feed that e-readers use). The server fetches pages of that feed with
HTTP Basic authentication, reads each entry's links (acquisition links for the files, image links for covers,
navigation links for sections, a `next` link and a search template), and passes the result to the page. Covers are
fetched through this server too, so the browser never needs access to Calibre-Web.

The code works from what the feed says, not from Calibre-Web's URL layout: search uses the template the catalogue
advertises (falling back to `/opds/search/{terms}`), and formats are recognised from the link's media type, then its
title, then the file extension.

## Safety

- The page only ever names a **path** on the configured server (`/opds/...`). The server joins it to the saved address
  and refuses anything else, so the page cannot make the server fetch from another host.
- Downloads over 300 MB are refused; an empty file is refused.
- The address is plain HTTP in most home setups, so the password and the book travel unencrypted between this server
  and Calibre-Web. Use an `https://` address if you have one.
- The web page itself has no login (see [Operations](operations.md)): anyone who can open it can browse your
  Calibre-Web library through it and import books.

## Messages and what they mean

| Message | Cause |
|---|---|
| "Calibre-Web rejected the username or password" | HTTP 401 or 403 |
| "Calibre-Web redirected to its login page…" | The server redirected instead of answering (wrong credentials, or OPDS switched off) |
| "Calibre-Web has no OPDS catalogue at that address. Switch on "Enable OPDS"…" | HTTP 404 under `/opds/` |
| "that address did not return an OPDS catalogue" | The address answered with something that is not an Atom feed (not Calibre-Web, a proxy page, …) |
| "cannot reach Calibre-Web at … Is it running, and is the address right?" | Network error |
| "that file is larger than 300 MB" / "Calibre-Web sent an empty file" | Download limits |
| "connect Calibre-Web in Settings first" (HTTP 409) | Nothing saved yet |

## What is and is not verified

The integration was tested against a stand-in server that follows the OPDS format (feeds, search, paging, covers,
downloads, Basic authentication, every failure above) and through the page. It was also run against one real
Calibre-Web instance on a local network: the catalogue parsed (titles, formats, covers, download links), books were
downloaded, and Calibre-Web's covers were fetched and compared with the ones inside the EPUBs. Search and paging on a
real instance, and the **Preview** button opening a book's page in a browser, were not tried. If your Calibre-Web words
something differently, **Test** is where it will show.
