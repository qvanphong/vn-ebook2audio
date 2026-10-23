# Golden files for the Nano model

Reference data from VieNeu 3.8.3 (`pnnbao-ump/VieNeu-TTS-v3-Nano`) for porting its inference to another runtime, such as
Android. Regenerate with `python tools/make_golden.py` (needs the full `vieneu` install). The generator recomputes the
engine's loop and refuses to write the files if the result differs from `engine.infer`.

`manifest.json` holds constants (sample rate 24000, frame rate 15.625, ids, chunk limits), the text steps for three
sentences, and a description of the numeric case.

**Text steps** (`cases`): for each sentence the chunks (max 140 characters), the pause after each chunk, the phoneme
string, the token ids, the predicted `log_seconds` and the frame count. A port that receives phonemes from the server only
needs the ids, durations and frames.

**Numeric cases** (`numeric`, keyed `short` and `two_chunks`): one chunk each, `short` about 1.7 s of audio and `two_chunks` about 7.4 s (a realistic chunk length), run with a fixed noise tensor so no random generator has to match. File names are `numeric_<case>_<name>`; the table uses the short names. All files are
raw little-endian arrays; `manifest.json` gives dtype and shape for each:

| File | What | Check |
|---|---|---|
| `ids.i64` | token ids `[1, L]` | exact |
| `style.f32`, `speaker.f32` | the voice (`[1, 50, 256]`, `[1, 192]`) | inputs |
| `null_ids.i64`, `null_style.f32`, `null_speaker.f32` | the unconditional inputs for guidance | inputs |
| `ctx.f32` | `text_encoder(ids, style)`, `[1, L, 256]` | max abs diff 1e-3 |
| `noise.f32` | starting noise `[1, 144, T]` | input |
| `first_step_v.f32` | guided `v` after the first step | max abs diff 1e-2 |
| `latent.f32` | `x` after 16 steps | correlation above 0.99 |
| `decoder_wav.f32` | `codec_decoder(x)` | correlation above 0.99 |
| `final_wav.f32` | after clipping, trimming and fading | length within 1%, correlation above 0.98 |

The loop: `x = noise`; for `i` in 0..15, `t = i / 16`; `v = v_uncond + 3.0 * (v_cond - v_uncond)` where the conditional call
uses the chunk's context and voice and the unconditional call uses the null ones; `x += (t_next - t) * v`. Then decode.
That is 35 ONNX runs: 1 text encoder, 1 duration predictor, 32 vector estimator, 1 codec decoder.
