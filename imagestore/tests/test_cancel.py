"""Cancellation and cleanup in the operations layer (Task / encode_path / decode_path)."""
from __future__ import annotations

import threading

import pytest

MiB = 1 << 20


def _names(folder):
    return sorted(p.name for p in folder.iterdir())


def test_preset_cancel_stops_encode_and_leaves_nothing(istore, tmp_path, rand_bytes):
    src = tmp_path / "a.bin"
    src.write_bytes(rand_bytes(100_000, seed=1))
    cancel = threading.Event()
    cancel.set()

    with pytest.raises(istore.Cancelled) as ei:
        istore.encode_path(src, tmp_path / "a.png", task=istore.Task(cancel=cancel))

    assert ei.value.kind == "cancelled"
    assert _names(tmp_path) == ["a.bin"]


def test_cancel_mid_encode_leaves_nothing(istore, tmp_path, rand_bytes):
    src = tmp_path / "big.bin"
    src.write_bytes(rand_bytes(4 * MiB, seed=11))
    cancel = threading.Event()

    def progress(label, done, total):
        if done > MiB:
            cancel.set()

    with pytest.raises(istore.Cancelled):
        istore.encode_path(src, tmp_path / "big.png", task=istore.Task(progress, cancel=cancel))

    assert _names(tmp_path) == ["big.bin"]


def test_cancel_during_decode_writes_no_output(istore, tmp_path, rand_bytes):
    src = tmp_path / "blob.bin"
    src.write_bytes(rand_bytes(3 * MiB, seed=5))
    png = tmp_path / "blob.png"
    istore.encode_path(src, png)
    out = tmp_path / "out"
    out.mkdir()
    cancel = threading.Event()

    def progress(label, done, total):
        if label.startswith("Decoding") and done > 0:
            cancel.set()

    with pytest.raises(istore.Cancelled):
        istore.decode_path(png, out, task=istore.Task(progress, cancel=cancel))

    assert list(out.iterdir()) == []  # no restored file, no .imagestore-*.part


def test_cancel_during_folder_decode_extracts_nothing(istore, tmp_path, rand_bytes):
    proj = tmp_path / "proj"
    (proj / "sub").mkdir(parents=True)
    (proj / "sub" / "big.bin").write_bytes(rand_bytes(3 * MiB, seed=6))
    (proj / "a.txt").write_text("alpha")
    png = tmp_path / "proj.png"
    istore.encode_path(proj, png)
    dest = tmp_path / "dest"
    dest.mkdir()
    cancel = threading.Event()

    def progress(label, done, total):
        if label.startswith("Decoding") and done > 0:
            cancel.set()

    with pytest.raises(istore.Cancelled):
        istore.decode_path(png, dest, task=istore.Task(progress, cancel=cancel))

    assert list(dest.rglob("*")) == []


def _labels_for(istore, src, png, **kwargs):
    labels: list[str] = []

    def progress(label, done, total):
        if not labels or labels[-1] != label:
            labels.append(label)

    istore.encode_path(src, png, task=istore.Task(progress), **kwargs)
    return labels


def test_progress_labels_for_compressed_encode(istore, tmp_path):
    src = tmp_path / "c.txt"
    src.write_bytes(b"abcdefgh" * 40_000)
    labels = _labels_for(istore, src, tmp_path / "c.png")

    assert labels[0] == "Hashing and compressing"
    assert labels.index("Writing PNG") > labels.index("Hashing and compressing")
    assert labels.index("Verifying written PNG") > labels.index("Writing PNG")
    assert labels[-1] == "Verifying written PNG"


def test_progress_labels_for_raw_storage_include_packing(istore, tmp_path, rand_bytes):
    src = tmp_path / "r.bin"
    src.write_bytes(rand_bytes(200 * 1024, seed=21))
    labels = _labels_for(istore, src, tmp_path / "r.png", compress=True)  # zlib does not help: stored raw

    assert labels[0] == "Hashing and compressing"
    assert "Packing pixels" in labels
    assert labels.index("Packing pixels") > labels.index("Hashing and compressing")
    assert labels.index("Packing pixels") < labels.index("Writing PNG")


def test_progress_labels_for_compress_off(istore, tmp_path, rand_bytes):
    src = tmp_path / "r.bin"
    src.write_bytes(rand_bytes(64 * 1024, seed=22))
    labels = _labels_for(istore, src, tmp_path / "r.png", compress=False)

    assert labels[0] == "Hashing"
    assert "Packing pixels" in labels
