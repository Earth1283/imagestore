"""Command-line companion to the eXact GUI.

    exact-cli info FILE
    exact-cli hash FILE
    exact-cli compare A B [--heatmap OUT.png]
    exact-cli roundtrip FILE              # load + verified save to a temp file, compare
    exact-cli make-test-image OUT [--size WxH] [--mode RGBA|RGB] [--seed N]
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile

import numpy as np

from . import __version__
from .core.diff import compare_files, describe_diff, compare_buffers
from .core.pixels import buffer_sha256
from .core.png_io import UnsupportedPngError, load_png, save_png
from .core.png_chunks import PngFormatError
from .core.synthetic import payload_image


def _info(args) -> int:
    loaded = load_png(args.file)
    lay = loaded.layout
    h, w, c = loaded.pixels.shape
    print(f"File:            {loaded.path}")
    print(f"File size:       {lay.file_size} bytes")
    print(f"File SHA-256:    {lay.file_sha256}")
    print(f"Dimensions:      {w}×{h}")
    print(f"Mode:            {loaded.mode} (8-bit, {c} bytes per pixel)")
    print(f"Interlace:       {'Adam7' if lay.ihdr.interlace else 'none'}")
    print(f"Pixel SHA-256:   {buffer_sha256(loaded.pixels)}")
    print("Chunks:          " + ", ".join(
        f"{n}×{k} ({b} B)" if k > 1 else f"{n} ({b} B)" for n, k, b in lay.summarized_sequence()))
    for warning in loaded.warnings:
        print(f"Warning:         {warning}")
    return 0


def _hash(args) -> int:
    loaded = load_png(args.file)
    print(f"{buffer_sha256(loaded.pixels)}  pixels  {loaded.path}")
    print(f"{loaded.layout.file_sha256}  file    {loaded.path}")
    return 0


def _compare(args) -> int:
    result = compare_files(args.a, args.b, heatmap=bool(args.heatmap))
    print("\n".join(result.report_lines()))
    if args.heatmap and result.diff is not None:
        heat = result.diff.heatmap
        rgb = np.zeros(heat.shape + (3,), dtype=np.uint8)
        rgb[..., 0] = np.where(heat > 0, 255, 0)
        rgb[..., 1] = heat
        save_png(args.heatmap, rgb)
        print(f"Heatmap written to {args.heatmap}")
    if result.mismatch:
        return 2
    return 0 if result.diff.identical else 1


def _roundtrip(args) -> int:
    loaded = load_png(args.file)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "roundtrip.png")
        report = save_png(out, loaded.pixels)
        again = load_png(out)
        d = compare_buffers(loaded.pixels, again.pixels, heatmap=False)
    print("\n".join(describe_diff(d)))
    print(f"Verified save took {report.elapsed:.2f}s; file bytes {'identical' if report.file_sha256 == loaded.layout.file_sha256 else 'differ (expected: re-encoded)'}")
    return 0 if d.identical else 1


def _make(args) -> int:
    w, h = (int(v) for v in args.size.lower().split("x"))
    arr = payload_image(w, h, args.mode, args.seed)
    report = save_png(args.out, arr)
    print(f"Wrote {report.dest} ({w}×{h} {args.mode}); pixel SHA-256 {report.pixel_sha256}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exact-cli", description=f"eXact {__version__} command-line tools")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info", help="show PNG structure, mode and hashes")
    p.add_argument("file")
    p.set_defaults(fn=_info)
    p = sub.add_parser("hash", help="print pixel-buffer and file SHA-256")
    p.add_argument("file")
    p.set_defaults(fn=_hash)
    p = sub.add_parser("compare", help="compare decoded pixels of two PNGs (exit 0 identical, 1 differ, 2 incomparable)")
    p.add_argument("a")
    p.add_argument("b")
    p.add_argument("--heatmap", metavar="OUT.png")
    p.set_defaults(fn=_compare)
    p = sub.add_parser("roundtrip", help="verified save to a temp file and confirm identical pixels")
    p.add_argument("file")
    p.set_defaults(fn=_roundtrip)
    p = sub.add_parser("make-test-image", help="write a deterministic binary-payload test PNG")
    p.add_argument("out")
    p.add_argument("--size", default="512x512")
    p.add_argument("--mode", choices=("RGBA", "RGB"), default="RGBA")
    p.add_argument("--seed", type=int, default=921)
    p.set_defaults(fn=_make)
    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except (UnsupportedPngError, PngFormatError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
