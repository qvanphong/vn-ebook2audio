"""A chapter's audio goes to ffmpeg piece by piece, so a long chapter is never held three times over in memory."""
import numpy as np
import pytest

from app.m4b import encode_chapter, probe_seconds


def tone(seconds, rate=24000):
    return (0.3 * np.sin(np.linspace(0, 440 * np.pi * seconds, int(rate * seconds)))).astype(np.float32)


def test_pieces_play_one_after_another_for_as_long_as_the_same_audio_in_one_array(tmp_path):
    pieces = [tone(1.0), np.zeros(12000, dtype=np.float32), tone(0.5), np.zeros(0, dtype=np.float32), tone(2.0)]
    whole = tmp_path / "whole.m4a"
    parts = tmp_path / "parts.m4a"
    encode_chapter(np.concatenate(pieces), 24000, whole, {"title": "Một"}, None)
    encode_chapter(iter(pieces), 24000, parts, {"title": "Một"}, None)  # a generator is fine too
    assert abs(probe_seconds(parts) - 4.0) < 0.1
    assert abs(probe_seconds(parts) - probe_seconds(whole)) < 0.05


def test_a_float64_piece_is_converted_rather_than_read_as_noise(tmp_path):
    out = tmp_path / "c.m4a"
    encode_chapter([tone(1.0).astype(np.float64)], 24000, out, {}, None)
    assert abs(probe_seconds(out) - 1.0) < 0.1


def test_a_failing_ffmpeg_raises_with_its_message_and_leaves_no_file(tmp_path):
    out = tmp_path / "c.m4a"
    with pytest.raises(RuntimeError, match="ffmpeg failed"):
        encode_chapter([tone(1.0)], 0, out, {}, None)  # a sample rate of 0 is refused by ffmpeg
    assert not out.exists() and not list(tmp_path.glob("*.part"))
