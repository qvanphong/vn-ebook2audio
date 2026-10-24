from __future__ import annotations

import numpy as np
import pytest
from ebooklib import epub

from app.config import Settings
from app.jobs import JobStore

FILLER = " ".join(["Đây là một câu dài để chương đủ số ký tự tối thiểu cho bộ lọc."] * 4)


class FakeEngine:
    sample_rate = 24000

    def __init__(self, fail_on: str | None = None, batch: int = 1):
        self.calls: list[str] = []
        self.dialog_calls: list[str] = []
        self.requests: list[list[str]] = []  # one entry per call to the machine, with the texts it was given
        self.fail_on = fail_on
        self.batch = batch

    def synthesize(self, text: str, dialog: bool = False) -> np.ndarray:
        return self.synthesize_many([text], dialog)[0]

    def synthesize_many(self, texts: list[str], dialog: bool = False) -> list[np.ndarray]:
        if self.fail_on and any(self.fail_on in text for text in texts):
            raise RuntimeError("boom")
        self.requests.append(list(texts))
        self.calls.extend(texts)
        if dialog:
            self.dialog_calls.extend(texts)
        return [(0.2 * np.sin(np.linspace(0, 440 * np.pi, n))).astype(np.float32)
                for n in (int(self.sample_rate * 0.01 * len(text)) for text in texts)]


@pytest.fixture
def settings(tmp_path):
    s = Settings(data_dir=tmp_path / "data", settle_seconds=0.0)
    s.ensure_dirs()
    return s


@pytest.fixture
def store(settings):
    return JobStore(settings.db)


@pytest.fixture
def make_epub(tmp_path):
    def build(name="book.epub", chapters=None, title="Sách thử", author="Tác giả Thử", cover=None, language="vi"):
        chapters = chapters or [
            ("Chương 1", ["Anh Nam sống ở TP. Hồ Chí Minh từ năm 2010.", "Anh làm việc chăm chỉ!", FILLER]),
            ("Chương 2", ["Sáng hôm sau, họ lên đường.", "Hết chương hai.", FILLER]),
        ]
        b = epub.EpubBook()
        b.set_identifier("t"); b.set_title(title); b.set_language(language); b.add_author(author)
        if cover:
            b.set_cover("cover.png", cover, create_page=False)
        items = []
        for i, (ct, paras) in enumerate(chapters):
            c = epub.EpubHtml(title=ct, file_name=f"c{i}.xhtml", lang=language)
            c.content = f"<html><body><h1>{ct}</h1>{''.join(f'<p>{p}</p>' for p in paras)}</body></html>"
            b.add_item(c); items.append(c)
        b.toc = tuple(items); b.add_item(epub.EpubNcx()); b.add_item(epub.EpubNav())
        b.spine = ["nav", *items]
        path = tmp_path / name
        epub.write_epub(str(path), b)
        return path
    return build


LONG = "Một câu đủ dài để đoạn văn này vượt qua ngưỡng tối thiểu của chương. " * 5


@pytest.fixture
def toc_epub(tmp_path):
    """Build an EPUB from {file name: html body} and a [(title, href)] table of contents (empty list = none)."""

    def build(files, toc, title="Sách thật", language="en", author="Dr. Blair T. Spalding", name="toc.epub"):
        b = epub.EpubBook()
        b.set_identifier("x"); b.set_title(title); b.set_language(language); b.add_author(author)
        items = {}
        for fname, body in files.items():
            c = epub.EpubHtml(title=fname, file_name=fname, lang="vi")
            c.content = f"<html><body>{body}</body></html>"
            b.add_item(c); items[fname] = c
        b.toc = tuple(epub.Link(href, t, href) for t, href in toc)
        b.add_item(epub.EpubNcx()); b.add_item(epub.EpubNav())
        b.spine = ["nav", *items.values()]
        path = tmp_path / name
        epub.write_epub(str(path), b)
        return path

    return build


class FakeHandle:
    """Stands in for a loaded VieNeu model."""

    def __init__(self, backend, model):
        self.backend, self.model = backend, model

    def list_preset_voices(self):
        return [("Mỹ Duyên — Nữ", "Mỹ Duyên"), ("Anh Khôi — Nam", "Anh Khôi")]

    def voice_ids(self):
        return [vid for _label, vid in self.list_preset_voices()] + list(self.backend.cloned)

    def infer(self, text, voice=None, temperature=0.8, **_):
        import numpy as np

        self.backend.calls.append((self.model, text, voice, temperature))
        rate = {"turbo": 48000, "english": 44100}.get(self.model, 24000)
        n = int(rate * 0.01 * len(text))
        return (0.4 * np.sin(np.linspace(0, 300 * np.pi, n))).astype(np.float32)

    def infer_batch(self, texts, voice=None, temperature=0.8, **_):
        self.backend.batch_calls.append((self.model, list(texts), voice, temperature))
        return [self.infer(text, voice=voice, temperature=temperature) for text in texts]

    def infer_phonemes(self, phonemes, voice=None, steps=None, cfg=None, speed=1.0, seed=None):
        import numpy as np

        from app.tts import Unsupported

        if self.model != "nano":
            raise Unsupported("phoneme input is only implemented for the nano model")
        self.backend.phoneme_calls.append((self.model, phonemes, voice, steps, cfg, speed, seed))
        n = int(24000 * 0.01 * len(phonemes))
        return (0.4 * np.sin(np.linspace(0, 300 * np.pi, n))).astype(np.float32)

    def add_voice(self, name, ref_audio):
        self.backend.cloned.append(name)

    def remove_voice(self, name):
        if name in self.backend.cloned:
            self.backend.cloned.remove(name)


class FakeBackend:
    def __init__(self):
        self.calls, self.cloned, self.loads, self.phoneme_calls = [], [], [], []
        self.batch_calls = []  # one entry per batch request, with every text in it

    def load(self, model):
        self.loads.append(model)
        return FakeHandle(self, model)


def write_wav(path, seconds=1.0, rate=24000):
    import wave

    import numpy as np

    samples = (0.3 * np.sin(np.linspace(0, 440 * np.pi * seconds, int(rate * seconds))) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(samples.tobytes())
    return path
