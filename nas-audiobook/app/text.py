"""Sentence segmentation, dialogue detection and pause timing (the pause table is in docs/book-processing.md)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .model import Chapter, Clip

PAUSE_SENTENCE = 480
PAUSE_PARA = 720
PAUSE_HEADING = 1300
PAUSE_QUESTION = 540
PAUSE_ELLIPSIS = 600
PAUSE_COLON = 380
PAUSE_COMMA = 300
PAUSE_OTHER = 360
LEAD_TRIM_MS = 120
MIN_PAUSE_MS = 40
CHAPTER_END_MS = 1000
MAX_CLIP_CHARS = 2500
SPLIT_HARD, SPLIT_HEAD, SPLIT_TAIL = 260, 120, 40
MERGE_MAX_CHARS = 300

ABBREV = frozenset(("tp tt ts ths pgs gs bs ks mr mrs ms dr st vs v tr q nxb tx jr sr inc ltd etc "
                    "prof gen col capt lt sgt gov rev hon mt ft cf approx").split())
DOTTED_ABBREV = re.compile(r"(?:^|[^\w.])(?:[A-Za-z]\.)+[A-Za-z]$")  # e.g, i.e, a.m, p.m, U.S, Ph.D
SENTENCE_END = re.compile(r"[.!?…]+[\"”’»)\]]*(?=\s|$)")
PAUSE_TAG = re.compile(r"\[(?:nghỉ|nghi|pause)(?:\s+(\d+(?:[.,]\d+)?)\s*(?:s|giây|giay|sec)?)?\s*\]", re.I)
URL = re.compile(r"https?://\S+|www\.\S+", re.I)
FOOTNOTE_MARKS = re.compile(r"⁽?[⁰¹²³⁴⁵⁶⁷⁸⁹]+⁾?|\[\d{1,3}\]|(?<=[\w.,!?”\"’)])\(\d{1,3}\)")
BULLETS = re.compile(r"\s*[•*]+\s*")
QUOTE_TAIL = re.compile(r"[\"”’»)\]*]+$")
LONE_NUMBER_OR_LETTER = re.compile(r"\d{1,3}|[IVXLCDM]{1,5}|[ivx]{1,4}|\w")


@dataclass(frozen=True)
class TextOptions:
    read_titles: bool = True
    dialogue: bool = False
    pairs: list[tuple[str, str]] = field(default_factory=list)


QUOTE_SPAN = re.compile(r'“([^”]+)”|«([^»]+)»|"([^"\n]+)"')
DIALOG_DASH = re.compile(r"^\s*[-–—]\s+")
MIN_DIALOG_WORDS = 3


def dialogue_segments(para: str) -> list[tuple[str, bool]]:
    """Split a paragraph into (text, is_dialogue). Quoted spans of 3+ words and dash-led lines are dialogue."""
    dash = DIALOG_DASH.match(para)
    if dash:
        return [(para[dash.end():], True)]
    out: list[tuple[str, bool]] = []
    pos = 0
    for m in QUOTE_SPAN.finditer(para):
        inner = next(g for g in m.groups() if g is not None)
        if words(inner) < MIN_DIALOG_WORDS:
            continue
        if m.start() > pos:
            out.append((para[pos:m.start()], False))
        out.append((inner, True))
        pos = m.end()
    if pos < len(para):
        out.append((para[pos:], False))
    return out


def load_pronunciations(path: Path) -> list[tuple[str, str]]:
    if not path.exists():
        return []
    pairs = []
    for line in path.read_text(encoding="utf8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            src, dst = line.split("=", 1)
            if src.strip():
                pairs.append((src.strip(), dst.strip()))
    return sorted(pairs, key=lambda p: -len(p[0]))


def apply_pronunciations(text: str, pairs: list[tuple[str, str]]) -> str:
    if not pairs:
        return text
    table = {a.lower(): b for a, b in pairs}
    pattern = "|".join(
        (r"(?<![\w])" if a[0].isalnum() else "") + re.escape(a) + (r"(?![\w])" if a[-1].isalnum() else "")
        for a, _ in pairs
    )
    return re.sub(pattern, lambda m: table[m.group().lower()], text, flags=re.I)


def clean(text: str) -> str:
    text = URL.sub("", text)
    text = FOOTNOTE_MARKS.sub("", text)
    text = BULLETS.sub(", ", text)
    text = re.sub(r"(?<=[\w.,!?…”\"’)])\s*/\s+(?=\w)", ", ", text)
    return re.sub(r"\s+", " ", text).strip()


def words(s: str) -> int:
    return len(s.split())


def pause_after(sentence: str) -> int:
    last = QUOTE_TAIL.sub("", sentence.strip())[-1:]
    if last in ("?", "!"):
        ms = PAUSE_QUESTION
    elif last == "…" or sentence.rstrip().endswith("..."):
        ms = PAUSE_ELLIPSIS
    elif last == ":":
        ms = PAUSE_COLON
    elif last in (",", ";"):
        ms = PAUSE_COMMA
    elif last == ".":
        ms = PAUSE_SENTENCE
    else:
        ms = PAUSE_OTHER
    return max(MIN_PAUSE_MS, ms - LEAD_TRIM_MS)


def paragraph_pause() -> int:
    return PAUSE_PARA - LEAD_TRIM_MS


def split_long(sentence: str) -> list[str]:
    if len(sentence) <= SPLIT_HARD:
        return _hard_wrap(sentence)
    parts, cur = [], 0
    for m in re.finditer(r"[,;:—–]\s+", sentence):
        if m.end() - cur >= SPLIT_HEAD and len(sentence) - m.end() >= SPLIT_TAIL:
            parts.append(sentence[cur:m.start() + 1])
            cur = m.end()
    parts.append(sentence[cur:])
    return [p for part in parts for p in _hard_wrap(part)]


def _hard_wrap(text: str) -> list[str]:
    out = []
    while len(text) > MAX_CLIP_CHARS:
        cut = text.rfind(" ", 0, MAX_CLIP_CHARS)
        cut = cut if cut > MAX_CLIP_CHARS // 2 else MAX_CLIP_CHARS
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    if text:
        out.append(text)
    return out


def split_sentences(text: str) -> list[str]:
    spans, start = [], 0
    for m in SENTENCE_END.finditer(text):
        head = text[start:m.start()].strip()
        word = (re.findall(r"\w+$", head) or [""])[0]
        tail_word = word.lower()
        initial = len(word) == 1 and word.isupper()
        dotted = bool(DOTTED_ABBREV.search(head))
        if m.group().startswith(".") and (tail_word in ABBREV or initial or dotted or LONE_NUMBER_OR_LETTER.fullmatch(head)):
            continue
        spans.append(text[start:m.end()].strip())
        start = m.end()
    if text[start:].strip():
        spans.append(text[start:].strip())
    pieces = [p for s in spans if s for p in split_long(s)]
    merged: list[str] = []
    for p in pieces:
        if merged and words(merged[-1]) <= 2 and len(merged[-1]) + len(p) + 1 <= MERGE_MAX_CHARS:
            merged[-1] = f"{merged[-1]} {p}"
        else:
            merged.append(p)
    return merged


def _speakable(s: str) -> bool:
    return re.search(r"\w", s) is not None


def paragraph_clips(para: str, pairs: list[tuple[str, str]], dialogue: bool = False) -> list[Clip]:
    if dialogue:
        return [c for text, flag in dialogue_segments(para) for c in _segment_clips(text, pairs, flag)]
    return _segment_clips(para, pairs, False)


def _segment_clips(para: str, pairs: list[tuple[str, str]], dialog: bool) -> list[Clip]:
    clips: list[Clip] = []
    cursor = 0
    segments = []
    for m in PAUSE_TAG.finditer(para):
        segments.append((para[cursor:m.start()], _tag_ms(m)))
        cursor = m.end()
    segments.append((para[cursor:], None))
    for chunk, explicit in segments:
        for sentence in split_sentences(apply_pronunciations(clean(chunk), pairs)):
            if _speakable(sentence):
                clips.append(Clip(sentence, pause_after(sentence), dialog))
        if explicit is not None and clips:
            clips[-1] = Clip(clips[-1].text, explicit, dialog)
    return clips


def _tag_ms(m: re.Match) -> int:
    value = float(m.group(1).replace(",", ".")) if m.group(1) else 1.0
    return int(min(10.0, max(0.1, value)) * 1000)


def build_chapter(title: str, text: str, opts: TextOptions) -> Chapter:
    clips: list[Clip] = []
    if opts.read_titles and _speakable(title):
        clips.append(Clip(clean(title), PAUSE_HEADING))
    paragraphs = [p for p in re.split(r"\n+", text) if p.strip()]
    for i, para in enumerate(paragraphs):
        para_clips = paragraph_clips(para, opts.pairs, opts.dialogue)
        if para_clips and i < len(paragraphs) - 1:
            last = para_clips[-1]
            para_clips[-1] = Clip(last.text, max(last.pause_ms, paragraph_pause()), last.dialog)
        clips.extend(para_clips)
    if clips:
        clips[-1] = Clip(clips[-1].text, CHAPTER_END_MS, clips[-1].dialog)
    return Chapter(title, tuple(clips))
