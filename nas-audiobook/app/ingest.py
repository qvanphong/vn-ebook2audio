"""Build a Book (spoken chapters) from a file using a book processor and a chapter selection."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .books import (SUPPORTED, IngestError, ParamError, Parsed, UnsupportedFormat, default_selection,  # noqa: F401
                    parse, text_hash)
from .model import Book, Plan
from .text import TextOptions, build_chapter


def build_book(parsed: Parsed, plan: Plan, opts: TextOptions) -> Book:
    indices: Sequence[int] = plan.selection if plan.selection is not None else default_selection(parsed.sections)
    chosen = [(i, parsed.sections[i]) for i in indices if 0 <= i < len(parsed.sections)]
    if not chosen:
        raise IngestError("no readable chapters found")
    chapters = tuple(
        replace(build_chapter(s.title or f"Chapter {i + 1}", s.text, opts), index=i, text_hash=text_hash(s.title, s.text), chars=s.chars)
        for i, s in chosen)
    return Book(plan.title or parsed.title, plan.author or parsed.author, parsed.language, parsed.cover, chapters)


def load_book(path: Path, opts: TextOptions, plan: Plan | None = None) -> Book:
    plan = plan or Plan()
    return build_book(parse(path, plan.processor, dict(plan.params)), plan, opts)
