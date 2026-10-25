"""Change how fast a clip is spoken without changing its pitch, for the models that have no speed of their own."""
from __future__ import annotations

import subprocess

import numpy as np


def change_speed(audio: np.ndarray, sample_rate: int, speed: float) -> np.ndarray:
    """The clip played `speed` times as fast (2.0 is half as long), through ffmpeg's tempo filter."""
    if speed == 1.0 or not audio.size:
        return audio
    proc = subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "f32le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
         "-filter:a", f"atempo={speed}", "-f", "f32le", "pipe:1"],
        input=audio.astype("<f4").tobytes(), capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"could not change the speed: {proc.stderr.decode(errors='replace')[-200:]}")
    return np.frombuffer(proc.stdout, dtype="<f4").astype(np.float32)
