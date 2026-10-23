"""Models whose weights sit in another folder of the Hugging Face cache must load with newer ONNX Runtime too."""
import os
import types
from pathlib import Path

import pytest

from app import onnxpaths

pytestmark = pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")


def hf_layout(tmp_path, model_bytes=b"graph ... vieneu_backbone_shared.data ... end", data_dir="bb", same_dir=False):
    """Files stored once under blobs/<xx>/, listed under snapshots/ as symlinks, like the Hugging Face cache."""
    blobs = tmp_path / "hub" / "blobs"
    snap = tmp_path / "hub" / "snapshots" / "rev" / "onnx_int8"
    snap.mkdir(parents=True)
    (blobs / "aa").mkdir(parents=True)
    (blobs / data_dir).mkdir(exist_ok=True)
    (blobs / "aa" / "model-blob").write_bytes(model_bytes)
    data_blob = (blobs / "aa" if same_dir else blobs / data_dir) / "data-blob"
    data_blob.write_bytes(b"w" * 4096)
    (blobs / "cc").mkdir(exist_ok=True)
    (blobs / "cc" / "cfg-blob").write_text("{}")
    os.symlink(blobs / "aa" / "model-blob", snap / "vieneu_prefill.onnx")
    os.symlink(data_blob, snap / "vieneu_backbone_shared.data")
    os.symlink(blobs / "cc" / "cfg-blob", snap / "config.json")
    return snap


def test_a_model_whose_weights_resolve_to_another_blob_folder_is_loaded_from_a_real_folder(tmp_path):
    snap = hf_layout(tmp_path)
    fixed = Path(onnxpaths.resolve(str(snap / "vieneu_prefill.onnx"), tmp_path / "real"))
    assert fixed.parent.parent == tmp_path / "real" and fixed.name == "vieneu_prefill.onnx"
    assert not fixed.is_symlink() and not (fixed.parent / "vieneu_backbone_shared.data").is_symlink()
    assert (fixed.parent / "vieneu_backbone_shared.data").read_bytes() == b"w" * 4096
    assert fixed.read_bytes().startswith(b"graph")
    assert not onnxpaths.escapes(fixed, onnxpaths.external_files(fixed))  # the check ONNX Runtime makes now passes
    assert not (fixed.parent / "config.json").exists()  # only what the model needs


def test_the_links_take_no_extra_disk_space_where_the_disk_allows_it(tmp_path):
    snap = hf_layout(tmp_path)
    fixed = Path(onnxpaths.resolve(str(snap / "vieneu_prefill.onnx"), tmp_path / "real"))
    assert os.stat(fixed.parent / "vieneu_backbone_shared.data").st_ino == os.stat(os.path.realpath(snap / "vieneu_backbone_shared.data")).st_ino


def test_without_hard_links_the_files_are_copied(tmp_path, monkeypatch):
    snap = hf_layout(tmp_path)

    def no_links(src, dst, **kw):
        raise OSError("cross-device link")

    monkeypatch.setattr(os, "link", no_links)
    fixed = Path(onnxpaths.resolve(str(snap / "vieneu_prefill.onnx"), tmp_path / "real"))
    assert (fixed.parent / "vieneu_backbone_shared.data").read_bytes() == b"w" * 4096


def test_the_second_call_reuses_the_folder(tmp_path):
    snap = hf_layout(tmp_path)
    first = onnxpaths.resolve(str(snap / "vieneu_prefill.onnx"), tmp_path / "real")
    inode = os.stat(Path(first).parent / "vieneu_backbone_shared.data").st_ino
    second = onnxpaths.resolve(str(snap / "vieneu_prefill.onnx"), tmp_path / "real")
    assert first == second and os.stat(Path(second).parent / "vieneu_backbone_shared.data").st_ino == inode
    assert len(list((tmp_path / "real").iterdir())) == 1


def test_weights_that_resolve_beside_the_model_need_no_change(tmp_path):
    snap = hf_layout(tmp_path, same_dir=True)
    path = str(snap / "vieneu_prefill.onnx")
    assert onnxpaths.resolve(path, tmp_path / "real") == path and not (tmp_path / "real").exists()


def test_a_model_that_names_no_other_file_needs_no_change(tmp_path):
    snap = hf_layout(tmp_path, model_bytes=b"a graph with its weights inside it")
    path = str(snap / "vieneu_prefill.onnx")
    assert onnxpaths.resolve(path, tmp_path / "real") == path


def test_bytes_other_extensions_and_missing_files_are_left_alone(tmp_path):
    assert onnxpaths.resolve(b"model bytes") == b"model bytes"
    other = tmp_path / "x.bin"
    other.write_bytes(b"x")
    assert onnxpaths.resolve(str(other)) == str(other)
    assert onnxpaths.resolve(str(tmp_path / "nope.onnx")) == str(tmp_path / "nope.onnx")
    plain = tmp_path / "m.onnx"
    plain.write_bytes(b"graph")
    assert onnxpaths.resolve(plain) == plain


def test_install_wraps_sessions_once_and_gives_them_the_fixed_path(tmp_path, monkeypatch):
    seen = []

    class InferenceSession:
        def __init__(self, path_or_bytes, sess_options=None, providers=None, **kw):
            seen.append((path_or_bytes, providers))

    ort = types.SimpleNamespace(InferenceSession=InferenceSession)
    monkeypatch.setattr(onnxpaths, "_installed", False)
    monkeypatch.setattr(onnxpaths, "cache_dir", lambda: tmp_path / "real")
    assert onnxpaths.install(ort) and onnxpaths.install(ort)
    wrapped = ort.InferenceSession
    assert wrapped is not InferenceSession and onnxpaths.install(ort) and ort.InferenceSession is wrapped
    snap = hf_layout(tmp_path)
    ort.InferenceSession(str(snap / "vieneu_prefill.onnx"), None, ["CPUExecutionProvider"])
    ort.InferenceSession(b"bytes")
    assert seen[0][0].startswith(str(tmp_path / "real")) and seen[0][1] == ["CPUExecutionProvider"] and seen[1][0] == b"bytes"


def test_the_cache_folder_sits_in_hf_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    assert onnxpaths.cache_dir() == tmp_path / "hf" / "onnx-real"


def test_the_local_backend_installs_the_fix_before_loading_a_model(monkeypatch):
    from app import tts

    called = []
    monkeypatch.setattr(onnxpaths, "install", lambda *a: called.append("install"))
    monkeypatch.setattr(tts.LocalBackend, "_load", lambda self, model: called.append("load") or types.SimpleNamespace(infer=lambda t, **k: None))
    tts.LocalBackend(1, "fp32").load("nano")
    assert called == ["install", "load"]
