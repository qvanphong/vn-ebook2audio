"""Short audio samples of a voice, cached on disk so choosing a voice is quick after the first listen."""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Mapping

import numpy as np

from .tts import SAMPLE_RATES

DEFAULT_TEXT = ("Xin chào, đây là giọng đọc của tôi. Hôm nay trời đẹp, chúng ta cùng nghe một câu chuyện nhé. "
                "Hello, this is how I sound when I read your book.")
ENGLISH_TEXT = ("Hello, this is how I sound when I read your book. The tide was coming in, and the story was only "
                "just beginning.")
MAX_TEXT_CHARS = 300


def default_text(model: str) -> str:
    return ENGLISH_TEXT if model == "english" else DEFAULT_TEXT


class SampleError(Exception):
    pass


def encode_mp3(audio: np.ndarray, sample_rate: int, out: Path) -> None:
    tmp = out.with_name(out.name + ".part")
    proc = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "-",
         "-c:a", "libmp3lame", "-q:a", "4", "-f", "mp3", str(tmp)],
        input=audio.astype("<f4").tobytes(), capture_output=True, check=False,
    )
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise SampleError(f"could not encode the sample: {proc.stderr.decode(errors='replace')[-200:]}")
    tmp.replace(out)


class SampleStore:
    def __init__(self, directory: Path):
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, model: str, voice: str | None, text: str, options: Mapping[str, float | int]) -> Path:
        key = hashlib.sha1(f"{model}|{voice}|{sorted(options.items())}|{text}".encode()).hexdigest()[:20]
        return self.dir / f"{key}.mp3"

    def get(self, pool, model: str, voice: str | None, text: str | None, options: Mapping[str, float | int]) -> Path:
        text = (text or default_text(model)).strip()
        if not text:
            raise SampleError("the sample text is empty")
        if len(text) > MAX_TEXT_CHARS:
            raise SampleError(f"the sample text is longer than {MAX_TEXT_CHARS} characters")
        path = self.path_for(model, voice, text, options)
        if not path.exists():
            audio = pool.synthesize(model, text, voice, options)
            encode_mp3(audio, SAMPLE_RATES[model], path)
        return path
