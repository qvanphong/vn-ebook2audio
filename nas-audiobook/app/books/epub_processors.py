from __future__ import annotations

import posixpath
from dataclasses import dataclass
from urllib.parse import unquote

from .base import ParamSpec, Section, Split
from .epub_text import (ANCHOR_RE, HEADING_RE, cut, drop_title_echo, first_heading, mark_anchors, mark_headings,
                        normalize_href, parse_document, spine_documents, squash, to_text)


@dataclass(frozen=True)
class TocEntry:
    title: str
    file: str
    fragment: str | None


def toc_entries(book) -> list[TocEntry]:
    out: list[TocEntry] = []

    def entry(link) -> TocEntry:
        file, frag = normalize_href(link.href)
        return TocEntry((link.title or "").strip(), file, frag)

    def walk(nodes) -> None:
        for node in nodes:
            if isinstance(node, tuple):
                head, children = node
                if getattr(head, "href", None):
                    out.append(entry(head))
                walk(children)
            elif getattr(node, "href", None):
                out.append(entry(node))

    walk(book.toc)
    return out


def _file_key(item) -> str:
    return posixpath.normpath(unquote(item.get_name()))


class EpubToc:
    id = "epub-toc"
    label = "EPUB table of contents"
    description = ("Uses the book's own table of contents for chapter titles and boundaries, including entries that "
                   "point inside a file. Best for well-made EPUBs.")
    params: tuple[ParamSpec, ...] = ()

    def accepts(self, source) -> bool:
        return source.kind == "epub"

    def split(self, source, params) -> Split:
        book = source.epub
        entries = toc_entries(book)
        if not entries:
            return Split([], "This book has no table of contents.")
        by_file: dict[str, list[tuple[int, str | None]]] = {}
        for i, e in enumerate(entries):
            by_file.setdefault(e.file, []).append((i, e.fragment))

        sections: list[tuple[str, list[str]]] = []
        for item in spine_documents(book):
            file_entries = by_file.get(_file_key(item), [])
            wanted = {i: f for i, f in file_entries if f}
            soup = parse_document(item)
            heading = first_heading(soup)
            found = mark_anchors(soup, wanted)
            pieces = cut(to_text(soup), ANCHOR_RE)
            missing = [i for i in wanted if i not in found]
            whole = next((i for i, f in file_entries if f is None), missing[0] if missing else None)
            for index, text in pieces:
                if index is None and whole is None:
                    if sections:
                        sections[-1][1].append(text)
                    continue
                owner = whole if index is None else index
                if owner is None:
                    continue
                sections.append((entries[owner].title or heading, [text]))
        return Split([Section(t, drop_title_echo(squash("\n".join(parts)), t)) for t, parts in sections])


class EpubSpine:
    id = "epub-spine"
    label = "One chapter per file"
    description = ("Each content file in the book's reading order becomes a chapter, named by its first heading or "
                   "the table of contents. Good for books where every chapter is its own file.")
    params: tuple[ParamSpec, ...] = ()

    def accepts(self, source) -> bool:
        return source.kind == "epub"

    def split(self, source, params) -> Split:
        book = source.epub
        toc_titles = {e.file: e.title for e in reversed(toc_entries(book)) if e.fragment is None and e.title}
        sections = []
        for item in spine_documents(book):
            soup = parse_document(item)
            text = squash(to_text(soup))
            if not text:
                continue
            title = first_heading(soup) or toc_titles.get(_file_key(item), "") or f"Section {len(sections) + 1}"
            sections.append(Section(title, drop_title_echo(text, title)))
        return Split(sections)


class EpubHeadings:
    id = "epub-headings"
    label = "Split at headings"
    description = ("Starts a new chapter at every heading tag, across files. Use it when chapters are marked with "
                   "h1/h2/h3 headings but the table of contents is missing or wrong.")
    params = (ParamSpec("levels", "Heading levels", "choice", "h1-h2", ("h1", "h1-h2", "h1-h3"),
                        "Which heading tags start a chapter."),)

    def accepts(self, source) -> bool:
        return source.kind == "epub"

    def split(self, source, params) -> Split:
        tags = {"h1": ["h1"], "h1-h2": ["h1", "h2"], "h1-h3": ["h1", "h2", "h3"]}[params["levels"]]
        sections: list[tuple[str, list[str]]] = [("(before the first heading)", [])]
        for item in spine_documents(source.epub):
            soup = parse_document(item)
            titles = mark_headings(soup, tags)
            for index, text in cut(to_text(soup), HEADING_RE):
                if index is None:
                    sections[-1][1].append(text)
                else:
                    sections.append((titles[index], [text]))
        return Split([Section(t, drop_title_echo(squash("\n".join(p)), t)) for t, p in sections if "".join(p).strip()])
