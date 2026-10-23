# Phone worker

Can a phone be a voice worker? This page records the scope, the experiment that was run on a Pixel 8 Pro, and what a
real Android app would still need. **No Android worker app exists.** The experiment (`android-spike/`) shows it is
feasible for the Nano model.

## The idea

A phone you already own does the heavy neural-network work while the NAS keeps the library, the page and the queue.
The NAS already speaks a small HTTP protocol to workers ([HTTP API](api.md#voice-worker-protocol-version-3)); a phone
would be one more machine in the list.

The biggest saving is to **do the text work on the NAS and make the phone run only the networks**. The text steps
(turning numbers and dates into words, chunking, phonemising) need the text code of the VieNeu project and its
dictionary. That code needs only numpy, not ONNX Runtime or PyTorch (`pip install --no-deps vieneu==3.8.3` plus
`sea-g2p`; a 127 MB environment instead of 724 MB), so the NAS can run it and send the phone one chunk of phonemes at a
time. A worker declares this with `capabilities.synthesize: false`; the server then calls `POST /infer`.
The Python worker can pretend to be a phone (`WORKER_ACCEPTS=phonemes`), so the whole path is tested without one.

## Why Nano only

| | Nano | Turbo |
|---|---|---|
| Size | 48 M parameters, about 270 MB of graphs (fp32) | Much larger; its backbone alone is 104 MB (int8) or 415 MB (fp32) |
| How it generates | Non-autoregressive: per chunk a text encoder, a duration predictor, 16 steps of a flow-matching loop with two network calls each, and a codec decoder, 35 ONNX runs in all | Autoregressive: for every audio frame one backbone step plus 16 small acoustic steps with caches, then a streaming codec |
| Output | 24 kHz | 48 kHz |
| Port size | About 250 lines of Kotlin for the loop | Many times that, with much more to get exactly right |

## The Nano loop (what a port must reproduce)

1. Phonemes to ids: look each character up in the model's `config.json` vocabulary, add begin and end ids, drop unknown
   characters.
2. `text_encoder(ids, style)` gives a context; `duration_predictor` gives the length: frames =
   `min(exp(log_seconds) / speed, 15 s) × 15.625`.
3. Start from random normal noise of shape `[1, 144, frames]`. Do 16 Euler steps: call `vector_estimator` once with
   the voice and once with the null voice, combine as `v = v_uncond + 3.0 × (v_cond − v_uncond)`, then
   `x += dt × v`.
4. `codec_decoder(x)` gives audio. Clip to ±1, trim silence at the edges, fade.

The graphs take a **boolean** mask (another export of the same model takes uint8, so a port must follow the graphs it
ships). Matching the Python output exactly needs the same noise, so `golden/` holds a fixed noise tensor and every
intermediate array for two chunks; `tools/make_golden.py` regenerates them and checks them against the library's own
output. See `nas-audiobook/golden/README.md` for the files and tolerances.

## What the experiment showed

A bare Android app loads the four graphs, runs the 35-call loop on the golden cases, and logs speed, temperature and
whether the results match. The same Kotlin code also runs on a desktop JVM to separate port bugs from phone problems.

**Parity.** On a Mac JVM and on the phone, in full precision, every compared array matches the Python reference (context,
first step and latent differ by 0.00; decoder audio correlates at 1.00000), for both golden cases.

**Speed** on a Pixel 8 Pro (Tensor G3, Android 16, charging, screen on, 4 threads unless stated, one 7.4 s chunk).
RTF is seconds of work per second of audio; below 1.0 is faster than real time.

| Setup | First runs | Settled | Notes |
|---|---|---|---|
| fp32, 2 threads | 0.75 | 1.67 | slower than real time |
| fp32, 4 threads | 0.48 | 0.97 | about real time |
| fp32, 8 threads | 0.79 | 0.95 | no better: four of the cores are slow |
| **int8, 4 threads** | **0.19** | **about 0.6** | 90 runs over 6 minutes |
| M1 Pro Mac, same code | 0.19 | 0.19 | fp32; int8 gave 0.11 |

**Why it slows.** Within about 11 seconds the fast cores reach 80 °C at 2.1–2.7 GHz and the phone throttles them to
about 1.1–1.3 GHz (the mid cores to 0.6–1.0 GHz), holding at 54–59 °C. Android's thermal status stayed at "light" the
whole time, so it never warned. After that the speed is steady, with no further decline over the longest run.

**What it means.** At RTF 0.6 a 300,000-character book (about 5.7 hours of audio) takes about 3.5 hours on the phone
alone, and about 5.5 hours in fp32. Memory is no problem (about 0.7 GB for the model; the phone had 3 GB free).

**int8.** Dynamic int8 quantisation of the two big graphs (vector estimator and codec decoder) shrinks the model from
271 MB to 101 MB and runs about 1.6 times faster on the phone once throttled (1.7 times on the Mac).
The audio is not identical to fp32 (waveform correlation 0.87). Measured on log-mel spectrograms with the same noise,
int8 differs from fp32 by 0.44, while two fp32 renders with different random seeds differ by 1.2 to 2.6, so int8
moves the audio about a quarter as much as ordinary run-to-run variation. **The int8 audio has not been listened to.**
`tools/quantize_nano.py` reproduces the conversion and the check.

**Not measured:** runs of hours, an unplugged phone, a closed case or a warm room, other Android versions, and the cost
of the server wrapper (HTTP, wake locks) around the model.

## What a real app still needs

| Part | Approach | Difficulty |
|---|---|---|
| Run the networks | ONNX Runtime for Android, CPU provider (avoid NNAPI at first: operator coverage and accuracy are uncertain) | Low |
| The loop and voices | The Kotlin port from the experiment; preset voices are JSON (a 192-number speaker vector and 50×256 style numbers each) | Low to medium |
| HTTP server | A foreground service with an embedded server (Ktor or NanoHTTPD), the token check and protocol 4 (`/health`, `/models`, `/voices`, `/infer`) | Medium |
| Staying alive | Foreground service, wake lock, Wi-Fi lock, battery-optimisation exemption, thermal handling. This is where phone workers usually fail | Medium to high |
| Models on the phone | Download on first use by the phone itself, or ship quantised copies | Medium |
| Cloning on the phone | Needs three more graphs (about 110 MB), a speaker encoder with filterbank features, a resampler, a denoiser. Skip at first; report `clone: false` | High |
| Turbo | Later, only if Nano is not good enough | High |

Rough effort: protocol and service 8–12 days, hardening 4–6 days (estimates, not measurements). The server needs no
change: it already treats a phone as a machine that takes phonemes, only runs Nano, and cannot clone.

## Risks

- **Heat over hours.** Six minutes settled and stayed settled; a long run, a closed case or a hot room could throttle
  further. Needs a long test.
- **Android power rules.** A server that must stay reachable with the screen off varies by phone and Android version.
- **Network.** The phone must be reachable from the NAS (same Wi-Fi or a VPN) and changes address; there is no
  discovery, you enter the address.
- **Model terms.** The upstream repository is Apache-2.0, but its README gives no separate terms for the weights, the
  codec or the pronunciation dictionary. Check the model cards before shipping an app that bundles them.
- **Version drift.** The Python code can change the pipeline. Pin the version and keep the golden files.

## Running the experiment

See `android-spike/README.md`: pair the phone over wireless debugging, build the APK (Gradle 8.13, JDK 17), and run
`./run-on-phone.sh <models folder> [threads] [runs]`. The script closes the app before each run, because Android hands a
new request to a screen that is still open and the run silently never starts.
