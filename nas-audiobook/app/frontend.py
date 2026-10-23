"""The text side of speech: numbers and dates to words, chunking, phonemes, and joining chunks of audio.

It is VieNeu's own code (vieneu_utils plus sea-g2p), which needs only numpy, not ONNX Runtime. A server that sends
phonemes to a worker that cannot do this itself (a phone) needs it installed:
    pip install --no-deps vieneu==3.8.3 && pip install sea-g2p"""
from __future__ import annotations

import threading

import numpy as np

NANO_MAX_CHARS = 140


class FrontendMissing(RuntimeError):
    pass


class Frontend:
    def __init__(self):
        try:
            from vieneu_utils import core_utils, phonemize_text
        except ImportError as exc:
            raise FrontendMissing(
                "this voice worker only accepts phonemes, and the text frontend is not installed on this server. "
                "Install it with: pip install --no-deps vieneu==3.8.3 && pip install sea-g2p") from exc
        self._core, self._text = core_utils, phonemize_text
        self._lock = threading.Lock()  # several workers share one frontend; the text work is quick next to the networks

    def chunks(self, text: str, max_chars: int = NANO_MAX_CHARS) -> tuple[list[str], list[str]]:
        with self._lock:
            return self._text.normalize_to_chunks_v3_with_gaps(text, max_chars=max_chars)

    def phonemes(self, chunk: str) -> str:
        with self._lock:
            return self._text.phonemize_text_with_emotions(chunk)

    def join(self, wavs: list[np.ndarray], sample_rate: int, gaps: list[str]) -> np.ndarray:
        if not wavs:
            return np.zeros(0, dtype=np.float32)
        return self._core.join_audio_chunks(wavs, sample_rate, silence_ps=self._core.gaps_to_silence(gaps))


_shared: Frontend | None = None
_shared_lock = threading.Lock()


def shared_frontend() -> Frontend:
    """One frontend for every phoneme-only worker, because its dictionary is large."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Frontend()
        return _shared
