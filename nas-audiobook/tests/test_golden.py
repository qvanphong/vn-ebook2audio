import json
from pathlib import Path

import numpy as np
import pytest

GOLDEN = Path(__file__).resolve().parent.parent / "golden"
DTYPES = {"<i8": 8, "<f4": 4}


def manifest():
    return json.loads((GOLDEN / "manifest.json").read_text(encoding="utf8"))


def cases():
    return list(manifest()["numeric"].items())


def test_there_is_a_short_and_a_realistic_length_numeric_case():
    frames = {k: v["frames"] for k, v in cases()}
    assert set(frames) == {"short", "two_chunks"} and frames["two_chunks"] > 100 > frames["short"]


@pytest.mark.parametrize("key", ["short", "two_chunks"])
def test_every_numeric_file_has_the_size_its_shape_and_dtype_promise(key):
    for spec in manifest()["numeric"][key]["files"].values():
        expected = int(np.prod(spec["shape"])) * DTYPES[spec["dtype"]]
        assert (GOLDEN / spec["file"]).stat().st_size == expected, spec["file"]


@pytest.mark.parametrize("key", ["short", "two_chunks"])
def test_the_numeric_case_is_internally_consistent(key):
    numeric = manifest()["numeric"][key]
    f = numeric["files"]
    ids = np.fromfile(GOLDEN / f["ids"]["file"], dtype="<i8")
    assert ids[0] == manifest()["bos_id"] and ids[-1] == manifest()["eos_id"]
    assert f["noise"]["shape"] == [1, 144, numeric["frames"]] and f["latent"]["shape"] == f["noise"]["shape"]
    final = np.fromfile(GOLDEN / f["final_wav"]["file"], dtype="<f4")
    assert 0 < np.abs(final).max() <= 1.0 and len(final) <= f["decoder_wav"]["shape"][0]


def test_golden_text_steps_still_match_the_installed_frontend():
    pytest.importorskip("sea_g2p")
    from vieneu_utils.core_utils import gaps_to_silence
    from vieneu_utils.phonemize_text import normalize_to_chunks_v3_with_gaps, phonemize_text_with_emotions

    for case in manifest()["cases"]:
        chunks, gaps = normalize_to_chunks_v3_with_gaps(case["text"], max_chars=140)
        assert chunks == case["chunks"] and gaps == case["gaps"] and gaps_to_silence(gaps) == case["silence_seconds"]
        assert [phonemize_text_with_emotions(c) for c in chunks] == case["phonemes"]
