"""Measure how fast this machine runs a voice model: python -m app.bench --model nano --threads 4
Compare devices on a GPU machine with --device cpu and --device cuda (the report says where the model really ran)."""
from __future__ import annotations

import argparse
import os
import sys
import time

from . import accel, catalog
from .config import available_cpus
from .samples import DEFAULT_TEXT
from .tts import MODEL_MODES, SAMPLE_RATES, ModelPool

CHARS_PER_SECOND = 14.5


def peak_mb() -> float | None:
    try:
        import resource  # not available on Windows
    except ImportError:
        return None
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / (1024 * 1024 if sys.platform == "darwin" else 1024)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", choices=sorted(MODEL_MODES), default="nano")
    ap.add_argument("--precision", choices=["fp32", "int8"], default="fp32", help="turbo only")
    ap.add_argument("--threads", type=int, default=0, help="0 means every available CPU")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--device", default="auto", help="auto (default), cpu, cuda, directml, coreml or openvino")
    a = ap.parse_args()
    threads = a.threads or available_cpus()
    catalog.configure_cache_env()
    try:
        accel.configure(a.device)
    except accel.DeviceError as exc:
        raise SystemExit(str(exc))

    t0 = time.time()
    pool = ModelPool.local(threads, a.precision)
    pool.get(a.model)
    load = time.time() - t0
    pool.synthesize(a.model, DEFAULT_TEXT, None, 0.8)
    rtfs = []
    for _ in range(a.runs):
        t = time.time()
        audio = pool.synthesize(a.model, DEFAULT_TEXT, None, 0.8)
        spent, seconds = time.time() - t, len(audio) / SAMPLE_RATES[a.model]
        rtfs.append(spent / seconds)
    rtf = sum(rtfs) / len(rtfs)
    print(f"model {a.model} ({a.precision if a.model == 'turbo' else 'fp32'}), {threads} threads, cpus visible {os.cpu_count()}")
    peak = peak_mb()
    placed = accel.status()["models"].get(a.model, {})
    print(f"device {placed.get('device', '?')}" + (f" ({placed['note']})" if placed.get("note") else "")
          + f", requested {a.device}")
    print(f"load {load:.1f} s" + (f", memory peak {peak:.0f} MB" if peak is not None else ""))
    print(f"speed: RTF {rtf:.2f} (seconds of work per second of audio; below 1.0 is faster than real time)")
    for chars, label in ((300_000, "300,000 characters (a short novel)"), (1_000_000, "1,000,000 characters (a long one)")):
        hours = chars / CHARS_PER_SECOND / 3600
        print(f"  {label}: about {hours:.1f} h of audio, about {hours * rtf:.1f} h to make")


if __name__ == "__main__":
    main()
