"""Turn EPUB documents into plain text with line breaks, and cut them at chosen anchors or headings."""
from __future__ import annotations

import posixpath
import re
from urllib.parse import unquote, urldefrag

import ebooklib
from bs4 import BeautifulSoup, NavigableString

BLOCKS = ["p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "section", "article",
          "pre", "dt", "dd", "figcaption", "table", "ul", "ol"]
JUNK = "script, style, nav, aside.footnote, [epub\\:type=footnote], [epub\\:type=noteref]"
HTML_TYPES = {"application/xhtml+xml", "text/html", "application/x-dtbook+xml"}
ANCHOR = ""
HEADING = ""
ANCHOR_RE = re.compile(f"{ANCHOR}(\\d+){ANCHOR}")
HEADING_RE = re.compile(f"{HEADING}(\\d+){HEADING}")
ECHO_WINDOW = 8
PREAMBLE_LINES = 3


def normalize_href(href: str) -> tuple[str, str | None]:
    path, frag = urldefrag(unquote(href))
    return posixpath.normpath(path), (frag or None)


def spine_documents(book):
    for idref, _ in book.spine:
        item = book.get_item_with_id(idref)
        if item is not None and (item.get_type() == ebooklib.ITEM_DOCUMENT or (item.media_type or "").lower() in HTML_TYPES):
            yield item


def parse_document(item) -> BeautifulSoup:
    """The document's body only, so head elements such as <title> are never spoken."""
    soup = BeautifulSoup(item.get_content(), "html.parser")
    body = soup.body or soup
    for junk in body.select(JUNK):
        junk.decompose()
    return body


def first_heading(soup) -> str:
    tag = soup.find(["h1", "h2", "h3"])
    return tag.get_text(" ", strip=True) if tag else ""


def mark_anchors(soup, ids: dict[int, str]) -> set[int]:
    found: set[int] = set()
    for index, frag in ids.items():
        target = soup.find(id=frag)
        if target is not None:
            target.insert_before(NavigableString(f"\n{ANCHOR}{index}{ANCHOR}\n"))
            found.add(index)
    return found


def mark_headings(soup, tags: list[str]) -> list[str]:
    titles: list[str] = []
    for tag in soup.find_all(tags):
        titles.append(tag.get_text(" ", strip=True))
        tag.insert_before(NavigableString(f"\n{HEADING}{len(titles) - 1}{HEADING}\n"))
    return titles


def to_text(soup) -> str:
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for tag in soup.find_all(BLOCKS):
        tag.insert_before(NavigableString("\n"))
        tag.insert_after(NavigableString("\n"))
    return soup.get_text("")


def cut(text: str, pattern: re.Pattern) -> list[tuple[int | None, str]]:
    """Split marked text into (marker number or None for the leading piece, text)."""
    parts = pattern.split(text)
    pieces: list[tuple[int | None, str]] = [(None, parts[0])]
    for i in range(1, len(parts), 2):
        pieces.append((int(parts[i]), parts[i + 1]))
    return pieces


def squash(text: str) -> str:
    lines = (re.sub(r"[ \t\r\f\v ]+", " ", ln).strip() for ln in text.replace(ANCHOR, "").replace(HEADING, "").split("\n"))
    return "\n".join(ln for ln in lines if ln)


def _key(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s, flags=re.UNICODE).strip().casefold()


def drop_title_echo(text: str, title: str) -> str:
    """Remove a line near the top that repeats the chapter title, since the title is read separately.

    Up to three lines above it (a running header such as author and book name) go with it."""
    want = _key(title)
    if not want:
        return text
    lines = text.split("\n")
    for i, ln in enumerate(lines[:ECHO_WINDOW]):
        if _key(ln) == want:
            return "\n".join(lines[i + 1:]) if i <= PREAMBLE_LINES else "\n".join(lines[:i] + lines[i + 1:])
    return text


def whole_text(book) -> str:
    """The whole book as plain text, one line per block, in reading order."""
    return "\n".join(squash(to_text(parse_document(item))) for item in spine_documents(book))
