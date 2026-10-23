from __future__ import annotations

import re

from .base import ParamError, ParamSpec, Section, Split

DEFAULT_PATTERN = (r"^\s*(?:#{1,3}\s+.+|(?:chương|chuong|phần|phan|hồi|quyển|chapter|part|book|section)"
                   r"\s+[\divxlc]+\b.{0,80})\s*$")
DEFAULT_CHUNK = "15000"


class TextPattern:
    id = "text-pattern"
    label = "Chapter title lines (pattern)"
    description = ("Reads the whole book as text and starts a chapter at every line that matches a pattern, such as "
                   "'Chương 3' or 'Chapter IV'. Works for any format, and for EPUBs whose chapter titles are plain "
                   "paragraphs. You can edit the pattern.")
    params = (ParamSpec("pattern", "Pattern (regular expression, case-insensitive)", "text", DEFAULT_PATTERN,
                        help="A line that matches starts a new chapter and becomes its title."),)

    def accepts(self, source) -> bool:
        return True

    def split(self, source, params) -> Split:
        try:
            rx = re.compile(params["pattern"], re.I)
        except re.error as exc:
            raise ParamError(f"the pattern is not a valid regular expression: {exc}") from None
        sections: list[tuple[str, list[str]]] = [("(before the first title line)", [])]
        for line in source.plain_text.split("\n"):
            if line.strip() and rx.match(line):
                sections.append((line.strip().lstrip("# ").strip(), []))
            else:
                sections[-1][1].append(line)
        out = [Section(t, "\n".join(ln for ln in (x.strip() for x in lines) if ln)) for t, lines in sections]
        return Split([s for s in out if s.text])


class FixedSize:
    id = "fixed-size"
    label = "Equal-sized parts"
    description = ("Cuts the text into parts of about the same length at paragraph boundaries. A last resort for books "
                   "with no usable structure; chapters are named Part 1, Part 2 and so on.")
    params = (ParamSpec("chars", "Characters per part", "number", DEFAULT_CHUNK,
                        help="About 15,000 characters is roughly 17 minutes of audio."),)

    def accepts(self, source) -> bool:
        return True

    def split(self, source, params) -> Split:
        limit = int(params["chars"])
        sections: list[Section] = []
        chunk: list[str] = []
        size = 0
        for line in (ln.strip() for ln in source.plain_text.split("\n")):
            if not line:
                continue
            chunk.append(line)
            size += len(line)
            if size >= limit:
                sections.append(Section(f"Part {len(sections) + 1}", "\n".join(chunk)))
                chunk, size = [], 0
        if chunk:
            sections.append(Section(f"Part {len(sections) + 1}", "\n".join(chunk)))
        return Split(sections)
