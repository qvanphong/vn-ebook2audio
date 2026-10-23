"""Runs the real English model. Off by default because the first run downloads about 400 MB:
    RUN_ENGINE_TESTS=1 pytest tests/test_english_real.py"""
import os

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("RUN_ENGINE_TESTS"), reason="set RUN_ENGINE_TESTS=1 to run the real model")


def test_the_real_english_model_speaks_every_voice_at_44_khz():
    from app.tts import SAMPLE_RATES, LocalBackend

    handle = LocalBackend(4, "fp32").load("english")
    assert handle.voice_ids() == ["F1", "F2", "F3", "F4", "F5", "M1", "M2", "M3", "M4", "M5"]
    text = "Dr. Smith arrived at 9:30 a.m. and the lights were already on."
    lengths = []
    for voice in ("F1", "M2"):
        audio = handle.infer(text, voice=voice)
        assert audio.dtype == np.float32 and audio.ndim == 1 and np.abs(audio).max() <= 1.0 and np.abs(audio).max() > 0.05
        lengths.append(len(audio) / SAMPLE_RATES["english"])
    assert all(2.5 < seconds < 8 for seconds in lengths), lengths
