"""Book processors: each one splits a loaded book into chapters in its own way."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..model import Cover
from .base import (MIN_CHAPTER_CHARS, ParamError, ParamSpec, Processor, Section, Split, default_selection,
                   resolve_params, selectable, text_hash)
from .epub_processors import EpubHeadings, EpubSpine, EpubToc
from .source import SUPPORTED, IngestError, Source, UnsupportedFormat, load_source
from .text_processors import FixedSize, TextPattern


class Auto:
    id = "auto"
    label = "Automatic (recommended)"
    description = ("Tries the table of contents, then headings, then one chapter per file, then title lines, and "
                   "keeps the first that finds at least two real chapters.")
    params: tuple[ParamSpec, ...] = ()

    def accepts(self, source) -> bool:
        return True

    def split(self, source, params) -> Split:
        for name in ("epub-toc", "epub-headings", "epub-spine", "text-pattern", "fixed-size"):
            proc = PROCESSORS[name]
            if not proc.accepts(source):
                continue
            split = proc.split(source, resolve_params(proc, None))
            if len([s for s in split.sections if selectable(s)]) >= 2 or name == "fixed-size":
                return Split(split.sections, f"Chose '{proc.label}'.")
        return Split([])


PROCESSORS: dict[str, Processor] = {p.id: p for p in (Auto(), EpubToc(), EpubSpine(), EpubHeadings(), TextPattern(), FixedSize())}


@dataclass(frozen=True)
class Parsed:
    title: str
    author: str
    language: str
    cover: Cover | None
    sections: list[Section]
    note: str
    processor: str
    params: dict[str, str]


def applicable(source: Source) -> list[Processor]:
    return [p for p in PROCESSORS.values() if p.accepts(source)]


def parse_source(source: Source, processor: str = "auto", params: dict[str, str] | None = None) -> Parsed:
    proc = PROCESSORS.get(processor)
    if proc is None:
        raise ParamError(f"unknown processor {processor!r}")
    if not proc.accepts(source):
        raise ParamError(f"'{proc.label}' cannot read a {source.path.suffix or 'this'} book")
    resolved = resolve_params(proc, params)
    split = proc.split(source, resolved)
    return Parsed(source.title, source.author, source.language, source.cover, split.sections, split.note, processor, resolved)


def parse(path: Path, processor: str = "auto", params: dict[str, str] | None = None,
          cache_dir: Path | None = None) -> Parsed:
    return parse_source(load_source(path, cache_dir), processor, params)
