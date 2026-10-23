from __future__ import annotations

import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .model import Cover

AAC_BITRATE = "64k"


@dataclass(frozen=True)
class ChapterRef:
    title: str
    path: Path
    seconds: float


def safe_name(s: str, fallback: str = "Unknown") -> str:
    s = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " ", s).strip(" .")
    return re.sub(r"\s+", " ", s)[:120] or fallback


def _meta_escape(s: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", s)


def _concat_quote(path: Path) -> str:
    """ffmpeg resolves relative entries against the list file's folder, so always write absolute paths."""
    return "'" + str(path.resolve()).replace("'", "'\\''") + "'"


def _run(cmd: list[str], tmp: Path, out: Path, stdin: bytes | None = None) -> None:
    proc = subprocess.run(cmd, input=stdin, capture_output=True, check=False)
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed: {proc.stderr.decode(errors='replace')[-400:]}")
    tmp.replace(out)


def encode_chapter(audio: np.ndarray, sample_rate: int, out: Path, tags: dict[str, str], cover: Cover | None) -> None:
    """Encode one chapter of mono float audio to an AAC .m4a with tags and optional cover, written atomically."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory() as d:
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "-"]
        if cover:
            img = Path(d) / f"cover.{cover.ext}"
            img.write_bytes(cover.data)
            cmd += ["-i", str(img)]
        for key, value in tags.items():
            cmd += ["-metadata", f"{key}={value}"]
        cmd += ["-map", "0:a"]
        if cover:
            cmd += ["-map", "1:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
        cmd += ["-c:a", "aac", "-b:a", AAC_BITRATE, "-movflags", "+faststart", "-f", "ipod", str(tmp)]
        _run(cmd, tmp, out, audio.astype("<f4").tobytes())


def retag(path: Path, tags: dict[str, str]) -> None:
    """Rewrite tags in place by copying the streams, so the audio is not re-encoded."""
    tmp = path.with_name(path.name + ".part")
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-map", "0", "-c", "copy", "-map_metadata", "0"]
    for key, value in tags.items():
        cmd += ["-metadata", f"{key}={value}"]
    cmd += ["-movflags", "+faststart", "-f", "ipod", str(tmp)]
    _run(cmd, tmp, path)


def replace_cover(path: Path, cover: Cover) -> None:
    """Put a different picture into an existing chapter file by copying its audio, so nothing is re-encoded."""
    tmp = path.with_name(path.name + ".part")
    with tempfile.TemporaryDirectory() as d:
        img = Path(d) / f"cover.{cover.ext}"
        img.write_bytes(cover.data)
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path), "-i", str(img), "-map", "0:a", "-map", "1:v",
               "-c", "copy", "-disposition:v", "attached_pic", "-map_metadata", "0", "-movflags", "+faststart",
               "-f", "ipod", str(tmp)]
        _run(cmd, tmp, path)


def probe_seconds(path: Path) -> float:
    proc = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, check=False)
    try:
        return float(proc.stdout.strip())
    except ValueError:
        raise RuntimeError(f"could not read the length of {path.name}") from None


def ffmetadata(title: str, author: str, language: str, chapters: list[ChapterRef]) -> str:
    lines = [";FFMETADATA1", f"title={_meta_escape(title)}", f"album={_meta_escape(title)}",
             f"artist={_meta_escape(author)}", f"album_artist={_meta_escape(author)}",
             "genre=Audiobook", f"language={_meta_escape(language)}"]
    t = 0
    for ch in chapters:
        end = t + round(ch.seconds * 1000)
        lines += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={t}", f"END={end}", f"title={_meta_escape(ch.title)}"]
        t = end
    return "\n".join(lines) + "\n"


def concat_m4b(chapters: list[ChapterRef], out: Path, title: str, author: str, language: str,
               cover: Cover | None) -> None:
    """Join chapter .m4a files (same codec and sample rate) into one chaptered M4B without re-encoding."""
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "list.txt").write_text("".join(f"file {_concat_quote(c.path)}\n" for c in chapters), encoding="utf8")
        (d / "meta.txt").write_text(ffmetadata(title, author, language, chapters), encoding="utf8")
        cmd = ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(d / "list.txt"),
               "-i", str(d / "meta.txt")]
        if cover:
            img = d / f"cover.{cover.ext}"
            img.write_bytes(cover.data)
            cmd += ["-i", str(img)]
        cmd += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        if cover:
            cmd += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
        cmd += ["-c:a", "copy", "-movflags", "+faststart", "-f", "ipod", str(tmp)]
        _run(cmd, tmp, out)
