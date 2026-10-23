"""Write reference data for porting the Nano voice model to another runtime (Android).

For a few sentences it records the text steps (chunks, pauses, phonemes, token ids) and, for one chunk, every number
the networks see and return, so a port can be checked stage by stage. It recomputes the engine's own loop and fails
if the result differs from engine.infer, so the files are known to match VieNeu itself.

    python tools/make_golden.py            # writes golden/
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "golden"
SEED = 1234
VOICE = "Mỹ Duyên"
SENTENCES = {
    "short": "Xin chào các bạn.",
    "numbers": "Hôm nay là ngày 22/10/2026, giá SP500 là 4.200,5 điểm!",
    "two_chunks": "Chúng ta cùng nghe một câu chuyện nhé, về hành trình của một người đi tìm sự thật giữa núi rừng xa xôi. "
                  "Trời đã tối, gió thổi mạnh, và đường về còn dài.",
}
NUMERIC_CASES = {"short": 0, "two_chunks": 0}   # sentence key -> which chunk to record in full


def save(name: str, array: np.ndarray, dtype: str) -> dict:
    array = np.ascontiguousarray(array, dtype=dtype)
    (OUT / name).write_bytes(array.tobytes())
    return {"file": name, "dtype": dtype, "shape": list(array.shape), "byte_order": "little"}


def run_engine(engine, phonemes, speaker, style, noise, steps=16, cfg=3.0, speed=1.0):
    """The loop from OnnxV3NanoEngine.infer, with the noise passed in and the intermediate values kept."""
    spk = speaker.reshape(1, -1).astype(np.float32)
    st = style.reshape(1, *style.shape[-2:]).astype(np.float32)
    ids = engine.encode_phones(phonemes)
    mask = ids != engine.pad
    ctx = engine.s_text.run(None, {"ids": ids, "style": st})[0]
    log_s = float(engine.s_dur.run(None, {"ctx": ctx, "ctx_mask": mask, "spk": spk})[0][0])
    secs = min(math.exp(log_s) / max(speed, 1e-3), 15.0)
    frames = max(2, int(round(secs * engine.fps)))
    x = noise.copy()
    tg = np.linspace(0.0, 1.0, steps + 1, dtype=np.float64)
    first_v = None
    for i in range(steps):
        t = np.array([tg[i]], dtype=np.float32)
        v = engine.s_ve.run(None, {"x": x, "t": t, "ctx": ctx, "ctx_mask": mask, "spk": spk, "style": st})[0]
        vu = engine.s_ve.run(None, {"x": x, "t": t, "ctx": engine._null_ctx, "ctx_mask": engine._null_mask,
                                    "spk": engine.null_spk, "style": engine.null_style})[0]
        v = vu + np.float32(cfg) * (v - vu)
        if first_v is None:
            first_v = v
        x = x + np.float32(tg[i + 1] - tg[i]) * v
    wav = engine.s_dec.run(None, {"x": x})[0][0, 0]
    return ids, ctx, log_s, frames, first_v, x, wav


def main() -> None:
    from vieneu import Vieneu
    from vieneu_utils.core_utils import gaps_to_silence, trim_and_fade
    from vieneu_utils.phonemize_text import normalize_to_chunks_v3_with_gaps, phonemize_text_with_emotions

    OUT.mkdir(exist_ok=True)
    tts = Vieneu(mode="v3nano", threads=4)
    engine = tts.engine
    speaker, style = tts._resolve_voice(VOICE, None)
    cases = []
    numeric = {}
    for key, text in SENTENCES.items():
        chunks, gaps = normalize_to_chunks_v3_with_gaps(text, max_chars=140)
        entry = {"id": key, "text": text, "chunks": chunks, "gaps": gaps, "silence_seconds": gaps_to_silence(gaps),
                 "phonemes": [phonemize_text_with_emotions(c) for c in chunks], "ids": [], "log_seconds": [], "frames": []}
        for ph in entry["phonemes"]:
            ids = engine.encode_phones(ph)
            ctx = engine.s_text.run(None, {"ids": ids, "style": style.reshape(1, *style.shape)})[0]
            log_s = float(engine.s_dur.run(None, {"ctx": ctx, "ctx_mask": ids != engine.pad,
                                                  "spk": speaker.reshape(1, -1)})[0][0])
            entry["ids"].append(ids[0].tolist())
            entry["log_seconds"].append(log_s)
            entry["frames"].append(max(2, int(round(min(math.exp(log_s), 15.0) * engine.fps))))
        cases.append(entry)
        if key in NUMERIC_CASES:
            ph = entry["phonemes"][NUMERIC_CASES[key]]
            frames = entry["frames"][NUMERIC_CASES[key]]
            noise = np.random.default_rng(SEED).standard_normal((1, 144, frames)).astype(np.float32)
            ids, ctx, log_s, _, first_v, latent, wav = run_engine(engine, ph, speaker, style, noise)
            expected = engine.infer(ph, speaker, style, seed=SEED)
            final = trim_and_fade(np.clip(wav, -1.0, 1.0).astype(np.float32), engine.SAMPLE_RATE)
            if not np.allclose(final, expected, atol=1e-5):
                raise SystemExit("the recomputed loop does not match engine.infer: the golden files would be wrong")
            p = f"numeric_{key}_"
            numeric[key] = {
                "case": key, "phonemes": ph, "seed": SEED, "voice": VOICE, "steps": 16, "cfg": 3.0, "speed": 1.0, "sway": 0.0,
                "euler_schedule": "t_i = i / steps; x += (t_{i+1} - t_i) * v; v = v_uncond + cfg * (v_cond - v_uncond)",
                "files": {
                    "ids": save(p + "ids.i64", ids, "<i8"),
                    "style": save(p + "style.f32", style, "<f4"),
                    "speaker": save(p + "speaker.f32", speaker, "<f4"),
                    "null_ids": save(p + "null_ids.i64", np.array([[engine.bos, engine.eos]]), "<i8"),
                    "null_style": save(p + "null_style.f32", engine.null_style, "<f4"),
                    "null_speaker": save(p + "null_speaker.f32", engine.null_spk, "<f4"),
                    "ctx": save(p + "ctx.f32", ctx, "<f4"),
                    "noise": save(p + "noise.f32", noise, "<f4"),
                    "first_step_v": save(p + "first_step_v.f32", first_v, "<f4"),
                    "latent": save(p + "latent.f32", latent, "<f4"),
                    "decoder_wav": save(p + "decoder_wav.f32", wav, "<f4"),
                    "final_wav": save(p + "final_wav.f32", final, "<f4"),
                },
                "log_seconds": log_s, "frames": frames,
                "tolerances": {"ctx": "max abs diff 1e-3", "first_step_v": "max abs diff 1e-2",
                               "decoder_wav": "correlation above 0.99 with the file", "final_wav": "length within 1% and correlation above 0.98"},
            }
    manifest = {
        "model": "pnnbao-ump/VieNeu-TTS-v3-Nano", "vieneu": "3.8.3", "sample_rate": engine.SAMPLE_RATE,
        "flow_fps": engine.fps, "group": engine.group, "bos_id": engine.bos, "eos_id": engine.eos, "pad_id": engine.pad,
        "max_chunk_seconds": 15.0, "max_chars_per_chunk": 140, "numeric_cases": list(NUMERIC_CASES), "voice_layout": {"speaker_emb": [192], "style": [50, 256]},
        "onnx_runs_per_chunk": "1 text_encoder + 1 duration_predictor + steps x 2 vector_estimator + 1 codec_decoder",
        "cases": cases, "numeric": numeric,
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf8")
    print("wrote", OUT, "with", len(cases), "cases; numeric cases:", ", ".join(numeric))


if __name__ == "__main__":
    main()
