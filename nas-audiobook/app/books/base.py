"""Shared types for book processors: a processor turns a loaded book into ordered sections (chapters)."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Literal, Protocol

MIN_CHAPTER_CHARS = 200
SKIP_TITLE = re.compile(r"^\s*(mục lục|table of contents|contents|copyright|bản quyền)\s*$", re.I)


class ParamError(ValueError):
    pass


@dataclass(frozen=True)
class Section:
    title: str
    text: str

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class ParamSpec:
    name: str
    label: str
    kind: Literal["text", "number", "choice"]
    default: str
    choices: tuple[str, ...] = ()
    help: str = ""


@dataclass(frozen=True)
class Split:
    sections: list[Section]
    note: str = ""


class Processor(Protocol):
    id: str
    label: str
    description: str
    params: tuple[ParamSpec, ...]

    def accepts(self, source) -> bool: ...

    def split(self, source, params: dict[str, str]) -> Split: ...


def resolve_params(processor: Processor, given: dict[str, str] | None) -> dict[str, str]:
    """Fill defaults and validate values against the processor's declared parameters."""
    given = given or {}
    out: dict[str, str] = {}
    for spec in processor.params:
        value = str(given.get(spec.name, spec.default)).strip() or spec.default
        if spec.kind == "number":
            try:
                if int(value) <= 0:
                    raise ValueError
            except ValueError:
                raise ParamError(f"{spec.label} must be a whole number above zero") from None
        elif spec.kind == "choice" and value not in spec.choices:
            raise ParamError(f"{spec.label} must be one of {', '.join(spec.choices)}")
        out[spec.name] = value
    return out


def text_hash(title: str, text: str) -> str:
    """Identity of a chapter as it is spoken (the title is read aloud), used to recognise existing audio."""
    return hashlib.sha1(f"{title}\n{text}".encode("utf8")).hexdigest()[:16]


def selectable(section: Section) -> bool:
    return section.chars >= MIN_CHAPTER_CHARS and not SKIP_TITLE.match(section.title)


def default_selection(sections: list[Section]) -> list[int]:
    return [i for i, s in enumerate(sections) if selectable(s)]
