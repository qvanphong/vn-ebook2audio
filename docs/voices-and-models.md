# Models and voices

## The models

| Id | Name | Language | Output | What it is like | Download |
|---|---|---|---|---|---|
| `nano` | Vietnamese · Nano | Vietnamese | 24 kHz | Fast and light. A flow-matching model; speaks English poorly | about 280 MB |
| `turbo` | Vietnamese · Turbo | Vietnamese | 48 kHz | Best Vietnamese quality, about three times slower than Nano. Autoregressive | about 620 MB (fp32) |
| `english` | English · Supertonic | English | 44.1 kHz | Clear and fast; 10 voices; no cloning | about 390 MB |

The Vietnamese models come from the VieNeu project (`pip install vieneu`, Apache-2.0); the English model is Supertonic
by Supertone (`pip install supertonic`, code under MIT, **weights under the OpenRAIL-M licence, which has use
restrictions**). Supertone has archived its repository, so the version is pinned (`supertonic==1.3.1`) and will not
get fixes.

The download sizes are rough figures kept in `catalog.py`; the page quotes them as "about N MB".

### Where models live

- VieNeu models are in the Hugging Face cache (`HF_HOME`, `/data/cache/hf` in Docker).
- The English model is in a `supertonic3` folder. When `HF_HOME` is set, that folder is kept inside it, so one mounted
  folder holds every model. Set `SUPERTONIC_CACHE_DIR` to put it somewhere else.
- A model is downloaded the first time anything asks for it. A machine "has" a model when its files are on disk
  (`downloaded`), and the model is "ready" when it is loaded in memory.

### Precision (Turbo only)

`TTS_PRECISION=fp32` (default) or `int8`. int8 is about four times smaller and meant for CPUs with VNNI; on the Apple
CPU tried it gave no speedup. The Nano model has no setting; an int8 copy exists only as an experiment for phones
(see [Phone worker](phone-worker.md)).

### Speed

Measured on an Apple M1 Pro with 4 threads, natively: Nano about 0.26 s of work per second of audio (RTF) and 700 MB of
memory; Turbo RTF 0.33 and about 1.6 GB; English RTF about 0.23. The same Nano model inside Docker Desktop on that Mac
ran about six times slower, so the runtime matters; measure with `python -m app.bench` on the machine you will use. No
number was measured on a small NAS CPU.

### Choosing for English

The English model was compared with the other two on the same ten English sentences by having a speech recogniser
(Whisper `small.en`) write down what it heard and counting word errors on the seven sentences without digits (119
words): English 1.7%, Turbo 7.6%, Nano 20.2%. That measures clarity, not whether a voice is pleasant, and ten
sentences is a small test. A two-chapter English book converted end to end through the page transcribed at 2.3% and
4.2% word errors.

## Which model a book starts with

- The language of the book (see [Book processing](book-processing.md)) decides: English starts on the English model,
  anything else on `TTS_MODEL` (default `nano`).
- The page switches automatically after an upload, and again if you upload a book in the other language. You can change
  the model by hand afterwards, and an explicit choice always wins.
- The drop folder and the quick API (`POST /api/jobs`) make the same choice from the book's language.
- `TTS_ENGLISH_MODEL=none` turns the automatic choice off; `TTS_ENGLISH_VOICE` gives English books a default voice.
- A Vietnamese default voice (`TTS_VOICE`) never carries over to an English book; the voice sets are different.

## Voices

Every model lists its voices at `GET /api/voices?model=...`, with `cloned: true` for cloned ones.

- **Vietnamese.** Nano has 11 preset voices (such as `Mỹ Duyên`, `Anh Khôi`, `Adam`); Turbo has 25.
- **English.** Ten: `F1`–`F5` (female) and `M1`–`M5` (male). The default is `F1`.
- **Narrator and dialogue voice.** A job has a narrator voice and an optional dialogue voice. With no dialogue voice
  everything is read by the narrator.
- **Voice list before the model loads.** The service keeps the last list it saw for each model on disk
  (`data/cache/voices.json`), so the list shows at once after a restart while the model loads in the background.
- **Samples.** `GET /api/voice-sample` returns a short MP3 of a voice, cached in `data/cache/samples`. The default
  sentence is Vietnamese (with an English line) for the Vietnamese models and English for the English model. You can
  pass your own text of up to 300 characters.

### Voice cloning

Upload a clean 3 to 15 second clip (any format ffmpeg reads; it is converted to 24 kHz mono and trimmed to 15 s).

- The clip is stored in `data/voices/` with an index, so a clone survives restarts, and it is enrolled into each
  Vietnamese model when that model loads.
- Cloning works for Nano and Turbo. The English model cannot clone, and a worker that only accepts phonemes (a phone)
  cannot either.
- The clone's name must not be a built-in voice's name.
- Voice clones are sent to a worker when a conversion on that worker needs them. Deleting a clone removes it from this
  server and tells every worker it can reach.
- Checked: the output is not silent. Not checked: that it sounds like the speaker.
- VieNeu can add an audio watermark, but only if the optional `resemble-perth` package (with PyTorch) is installed.
  This service does not install it, so audio is not watermarked.

### What makes voice settings "the same"

Two conversions share files only if the model, narrator, dialogue voice, voice settings and read-titles setting are
all equal. The file name carries the first six characters of a hash of those values (`Voice.signature()`), and the
library groups files by it. Only a setting changed from the model's own value counts, so audio made with the defaults
keeps its signature when a model gains a setting.

### Voice settings of each model

The page shows the settings of the chosen model under **Model**; an empty box means the model's own value. A preset,
a conversion, a worker and the library's record of each audio file keep every one. They are named as the engines name
them (`app/params.py` is the table, `GET /api/params` serves it).

| Model | Settings |
|---|---|
| Vietnamese · Nano | `speed`, `steps`, `cfg`, `sway`, `seed`, `max_chars` |
| Vietnamese · Turbo | `speed` (done to the finished audio, since the model has none), `temperature` (expressiveness), `top_k`, `top_p`, `repetition_penalty`, `repetition_window`, `max_new_frames`, `max_chars` |
| English · Supertonic | `speed`, `total_steps`, `silence_duration`, `max_chunk_length` |
