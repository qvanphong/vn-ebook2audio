"""Make the smaller int8 copy of the Nano graphs used for phones, and check how far it moves the audio.

    python tools/quantize_nano.py <folder with the four fp32 .onnx files> <output folder>

Only the two big graphs are quantised (dynamic int8 on MatMul and Gemm), which cuts 271 MB to about 101 MB. The check renders
the golden "short" and "two_chunks" chunks with the same noise and compares log-mel spectrograms, using two fp32 renders with
different random seeds as the yardstick for normal variation. It cannot say how the audio sounds: listen to it before relying on it.
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
GOLDEN = HERE.parent / "golden"


def main(src: Path, dst: Path) -> None:
    import librosa
    import onnxruntime as ort
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from vieneu import Vieneu

    from make_golden import VOICE, run_engine

    dst.mkdir(parents=True, exist_ok=True)
    for name in ("text_encoder", "duration_predictor"):
        shutil.copy(src / f"{name}.onnx", dst / f"{name}.onnx")
    for name in ("vector_estimator", "codec_decoder"):
        quantize_dynamic(str(src / f"{name}.onnx"), str(dst / f"{name}.onnx"), weight_type=QuantType.QInt8,
                         op_types_to_quantize=["MatMul", "Gemm"])
    size = lambda d: sum(f.stat().st_size for f in d.glob("*.onnx")) / 1048576
    print(f"fp32 {size(src):.0f} MB, int8 {size(dst):.0f} MB")

    tts = Vieneu(mode="v3nano", threads=4)
    engine = tts.engine
    speaker, style = tts._resolve_voice(VOICE, None)
    manifest = json.loads((GOLDEN / "manifest.json").read_text(encoding="utf8"))
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    q_ve = ort.InferenceSession(str(dst / "vector_estimator.onnx"), options, providers=["CPUExecutionProvider"])
    q_dec = ort.InferenceSession(str(dst / "codec_decoder.onnx"), options, providers=["CPUExecutionProvider"])
    fp_ve, fp_dec = engine.s_ve, engine.s_dec

    def mel(w):
        return np.log(librosa.feature.melspectrogram(y=w.astype(np.float32), sr=24000, n_fft=1024, hop_length=256, n_mels=80) + 1e-5)

    def distance(a, b):
        x, y = mel(a), mel(b)
        n = min(x.shape[1], y.shape[1])
        return float(np.abs(x[:, :n] - y[:, :n]).mean())

    for key, case in manifest["numeric"].items():
        frames = case["frames"]
        noise = lambda seed: np.random.default_rng(seed).standard_normal((1, 144, frames)).astype(np.float32)

        def render(seed, ve, dec):
            engine.s_ve, engine.s_dec = ve, dec
            return run_engine(engine, case["phonemes"], speaker, style, noise(seed))[-1]

        base = render(1234, fp_ve, fp_dec)
        print(f"[{key}] normal variation (other seeds): {distance(base, render(99, fp_ve, fp_dec)):.2f}, "
              f"{distance(base, render(7, fp_ve, fp_dec)):.2f}; int8 vs fp32: {distance(base, render(1234, q_ve, q_dec)):.2f}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
