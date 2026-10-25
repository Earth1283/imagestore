#!/usr/bin/env python3
"""ImageStore - stash any file, folder or string inside an RGBA PNG, using all four channels (alpha too).

    imagestore.py                      interactive menu
    imagestore.py encode FILE_OR_FOLDER [-o OUT] [--no-compress] [-f]
    imagestore.py encode -t "some text" [-o OUT]
    imagestore.py decode IMAGE [-o DIR] [-f]
    imagestore.py info IMAGE
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import hmac
import io
import math
import os
import struct
import sys
import tarfile
import tempfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from PIL import Image
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table
from rich.text import Text

MAGIC = b"IMST"
VERSION = 2  # v2: SHA-256 digest replaced the v1 CRC32
FLAG_ZLIB = 0x01
FLAG_TEXT = 0x02  # payload is UTF-8 text: decode prints it instead of writing a file
FLAG_DIR = 0x04   # payload is an uncompressed tar of a folder
# magic, version, flags, original size, stored size, sha256 of original, name length
HEADER = struct.Struct(">4sBBQQ32sH")
TEXT_NAME = "text.txt"
# matched against entry names (never the root folder itself) when auto-ignore is on
IGNORE_PATTERNS = (".DS_Store", "._*", "Thumbs.db", "desktop.ini", ".Trashes", ".Spotlight-V100",
                   "node_modules", "__pycache__", "*.pyc", ".git", ".venv", "venv")
BPP = 4  # R, G, B, A: every byte of every channel carries data
CHUNK = 1 << 20  # streaming granularity: hashing, (de)compression and pixel reads work in pieces this size
SPOOL = 8 << 20  # scratch data stays in RAM up to this size, then spills to a temp file

console = Console()


class ImageStoreError(Exception):
    pass


@dataclass
class Meta:
    name: str
    flags: int
    orig_size: int
    stored_size: int
    digest: bytes

    @property
    def compressed(self) -> bool:
        return bool(self.flags & FLAG_ZLIB)

    @property
    def is_text(self) -> bool:
        return bool(self.flags & FLAG_TEXT)

    @property
    def is_dir(self) -> bool:
        return bool(self.flags & FLAG_DIR)


# --------------------------------------------------------------------------- core


def _spool() -> BinaryIO:
    return tempfile.SpooledTemporaryFile(max_size=SPOOL)


def encode(src: BinaryIO, name: str, compress: bool = True, kind: int = 0) -> tuple[Image.Image, Meta]:
    """Build the image from a seekable binary stream, holding only the pixel buffer in memory."""
    src.seek(0)
    sha, size = hashlib.sha256(), 0
    packed = co = None
    packed_size = 0
    if compress:
        packed, co = _spool(), zlib.compressobj(9)
    while chunk := src.read(CHUNK):
        sha.update(chunk)
        size += len(chunk)
        if co:
            packed_size += packed.write(co.compress(chunk))
    flags = kind
    if co:
        packed_size += packed.write(co.flush())
    if co and packed_size < size:
        flags |= FLAG_ZLIB
        payload, stored = packed, packed_size
    else:
        payload, stored = src, size

    name_b = name.encode("utf-8")
    meta = Meta(name, flags, size, stored, sha.digest())
    head = HEADER.pack(MAGIC, VERSION, flags, size, stored, meta.digest, len(name_b)) + name_b

    pixels = math.ceil((len(head) + stored) / BPP)
    width = math.ceil(math.sqrt(pixels))
    height = math.ceil(pixels / width)
    buf = bytearray(width * height * BPP)  # zero-filled, so the padding needs no extra pass
    buf[:len(head)] = head
    view, pos, end = memoryview(buf), len(head), len(head) + stored
    payload.seek(0)
    while pos < end:
        n = payload.readinto(view[pos:end])
        if not n:
            raise ImageStoreError("source changed size while it was being encoded")
        pos += n
    if packed:
        packed.close()
    # RGBA maps the buffer in place instead of copying it
    return Image.frombuffer("RGBA", (width, height), buf, "raw", "RGBA", 0, 1), meta


class _Pixels:
    """Sequential reader over an image's raw RGBA bytes, fetched a band of rows at a time so the
    whole image is never duplicated as one bytes object."""

    def __init__(self, img: Image.Image):
        if img.mode != "RGBA":
            raise ImageStoreError(f"image mode is {img.mode}, expected RGBA (was the alpha channel stripped?)")
        self.img = img
        self.total = img.width * img.height * BPP
        self._bands = self._iter_bands()
        self._buf = memoryview(b"")

    def _iter_bands(self) -> Iterator[bytes]:
        w, h = self.img.size
        rows = max(1, CHUNK // (w * BPP))
        for y in range(0, h, rows):
            yield self.img.crop((0, y, w, min(y + rows, h))).tobytes()

    def read(self, n: int) -> memoryview | bytes:
        """Up to n bytes; fewer only at the end of the image."""
        parts, got = [], 0
        while got < n:
            if not self._buf:
                self._buf = memoryview(next(self._bands, b""))
                if not self._buf:
                    break
            part = self._buf[:n - got]
            self._buf = self._buf[len(part):]
            parts.append(part)
            got += len(part)
        return parts[0] if len(parts) == 1 else b"".join(parts)


def _payload(px: _Pixels, stored: int) -> Iterator[bytes]:
    left = stored
    while left:
        chunk = px.read(min(left, CHUNK))
        if not chunk:
            raise ImageStoreError("image is truncated (payload extends past the pixel data)")
        left -= len(chunk)
        yield chunk


def _inflate(chunks: Iterator[bytes], orig_size: int) -> Iterator[bytes]:
    # output is capped at the declared size so a bogus header can't balloon memory
    d, left = zlib.decompressobj(), orig_size
    try:
        for chunk in chunks:
            while chunk and left and not d.eof:
                out = d.decompress(chunk, min(left, CHUNK))
                rest = d.unconsumed_tail
                if not out and len(rest) == len(chunk):
                    break  # no progress: let the size check report it
                chunk, left = rest, left - len(out)
                if out:
                    yield out
    except zlib.error as e:
        raise ImageStoreError(f"corrupt compressed payload: {e}") from e


def _verified(meta: Meta, chunks: Iterator[bytes]) -> Iterator[bytes]:
    sha, n = hashlib.sha256(), 0
    for chunk in chunks:
        sha.update(chunk)
        n += len(chunk)
        yield chunk
    if n != meta.orig_size:
        raise ImageStoreError(f"size mismatch: expected {meta.orig_size} bytes, got {n}")
    if not hmac.compare_digest(sha.digest(), meta.digest):
        raise ImageStoreError("SHA-256 mismatch: pixel data was altered (lossy re-encode or alpha premultiplication?)")


def decode(img: Image.Image) -> tuple[Meta, Iterator[bytes]]:
    """Parse the header and return (meta, original bytes in chunks).

    The size and SHA-256 checks run when the iterator is exhausted and raise ImageStoreError, so
    consume it fully before trusting the data (write to scratch space, then commit).
    """
    px = _Pixels(img)
    head = px.read(HEADER.size)
    if len(head) < HEADER.size:
        raise ImageStoreError("image too small to contain an ImageStore header")
    magic, version, flags, orig, stored, digest, name_len = HEADER.unpack(head)
    if magic != MAGIC:
        raise ImageStoreError("not an ImageStore image (bad magic)")
    if version != VERSION:
        raise ImageStoreError(f"unsupported ImageStore version {version}")
    if HEADER.size + name_len + stored > px.total:
        raise ImageStoreError("image is truncated (payload extends past the pixel data)")
    name = bytes(px.read(name_len)).decode("utf-8", errors="replace")
    meta = Meta(name, flags, orig, stored, digest)
    chunks = _payload(px, stored)
    return meta, _verified(meta, _inflate(chunks, orig) if meta.compressed else chunks)


def is_ignored(name: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pat) for pat in IGNORE_PATTERNS)


def scan_ignored(path: Path) -> tuple[list[Path], int, int]:
    """Find what auto-ignore would skip. Returns (top-level hits, files inside them, bytes)."""
    hits: list[Path] = []
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(path):
        here = Path(dirpath)
        for n in [n for n in dirnames if is_ignored(n)]:
            dirnames.remove(n)  # don't descend: the whole subtree is skipped
            hits.append(here / n)
            for dp, _, fns in os.walk(here / n):
                for fn in fns:
                    files += 1
                    try:
                        size += (Path(dp) / fn).lstat().st_size
                    except OSError:
                        pass
        for n in [n for n in filenames if is_ignored(n)]:
            hits.append(here / n)
            files += 1
            try:
                size += (here / n).lstat().st_size
            except OSError:
                pass
    return hits, files, size


def pack_dir(path: Path, ignore: bool = False) -> tuple[BinaryIO, int]:
    """Tar a folder into scratch space (uncompressed: zlib/PNG handle that). Returns (tar stream, file count)."""
    files = 0
    root = path.name or "folder"

    def scrub(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        nonlocal files
        if ignore and ti.name != root and is_ignored(PurePosixPath(ti.name).name):
            return None  # returning None for a directory skips its whole subtree
        files += ti.isfile()
        ti.uid = ti.gid = 0  # don't leak local user/group names into the image
        ti.uname = ti.gname = ""
        return ti

    tmp = _spool()
    with tarfile.open(fileobj=tmp, mode="w:") as tar:
        tar.add(path, arcname=root, filter=scrub)
    return tmp, files


def unpack_dir(chunks: Iterator[bytes], dest: Path) -> int:
    """Extract with the 'data' filter: blocks absolute paths, '..' escapes, devices, unsafe links.

    The tar is staged in scratch space first, so nothing lands in dest unless the image verified.
    """
    with _spool() as tmp:
        for chunk in chunks:
            tmp.write(chunk)
        tmp.seek(0)
        try:
            with tarfile.open(fileobj=tmp, mode="r:") as tar:
                files = sum(m.isfile() for m in tar.getmembers())
                tar.extractall(dest, filter="data")
        except (tarfile.TarError, OSError) as e:
            raise ImageStoreError(f"cannot extract folder: {e}") from e
    return files


def write_file(chunks: Iterator[bytes], out: Path) -> None:
    """Stream to a sibling temp file and rename on success, so a failed check never leaves bad output."""
    tmp = out.with_name(f".imagestore-{os.getpid()}.part")
    try:
        with open(tmp, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def open_image(path: Path) -> Image.Image:
    try:
        img = Image.open(path)
        img.load()
    except (OSError, Image.DecompressionBombError) as e:
        raise ImageStoreError(f"cannot open {path}: {e}") from e
    return img


def safe_name(name: str) -> str:
    """Stored names come from an untrusted image: keep the basename only."""
    base = Path(name.replace("\\", "/")).name
    return base if base not in ("", ".", "..") else "output.bin"


# ---------------------------------------------------------------------------- ui


def human(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return str(n)


def report(title: str, rows: list[tuple[str, str]], style: str = "green") -> None:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column()
    for k, v in rows:
        table.add_row(k, v)
    console.print(Panel(table, title=title, border_style=style, expand=False))


def default_png_path(src: Path) -> Path:
    return src.with_name(src.name + ".png")


def _write_png(data: BinaryIO, name: str, out: Path, compress: bool, force: bool,
               source: str, kind: int = 0) -> None:
    if out.exists() and not force:
        raise ImageStoreError(f"{out} already exists (use -f to overwrite)")

    with console.status(f"Encoding {escape(name)}…"):
        img, meta = encode(data, name, compress, kind)
        img.save(out, format="PNG", compress_level=1)

    w, h = img.size
    used = HEADER.size + len(meta.name.encode()) + meta.stored_size
    ratio = f"{meta.stored_size / meta.orig_size:.1%}" if meta.orig_size else "n/a"
    report("Encoded", [
        ("Source", source),
        ("Image", f"{escape(str(out))}  ({w}×{h} RGBA, {human(out.stat().st_size)} on disk)"),
        ("Data", f"{human(meta.orig_size)} → {human(meta.stored_size)} stored "
                 f"({'zlib, ' + ratio if meta.compressed else 'raw'})"),
        ("Pixels", f"{w * h:,} ({BPP} bytes each, {human(w * h * BPP - used)} padding)"),
        ("SHA-256", meta.digest.hex()),
    ])


def ask_ignore(src: Path) -> bool:
    """Scan for junk and ask whether to skip it. Nothing found -> no question."""
    with console.status("Scanning for ignorable files…"):
        hits, files, size = scan_ignored(src)
    if not hits:
        return False
    names = ", ".join(escape(f"{h.relative_to(src)}{'/' if h.is_dir() else ''}") for h in hits[:5])
    more = f" … +{len(hits) - 5} more" if len(hits) > 5 else ""
    console.print(f"[yellow]Auto-ignore[/] would skip {len(hits)} entries "
                  f"({files:,} files, {human(size)}): {names}{more}")
    return Confirm.ask("Skip them?", default=True)


def do_encode(src: Path, out: Path | None, compress: bool, force: bool,
              ignore: bool | None = None) -> None:
    """ignore: True/False decides auto-ignore for folders; None asks."""
    if src.is_dir():
        src = src.resolve()
        if ignore is None:
            ignore = ask_ignore(src)
        with console.status(f"Packing {escape(src.name)}/…"):
            tar, files = pack_dir(src, ignore)
        with tar:
            _write_png(tar, src.name or "folder", out or default_png_path(src), compress, force,
                       f"folder {escape(str(src))}/ ({files:,} files{', auto-ignore on' if ignore else ''})",
                       FLAG_DIR)
    elif src.is_file():
        with src.open("rb") as f:
            _write_png(f, src.name, out or default_png_path(src), compress, force, escape(str(src)))
    else:
        raise ImageStoreError(f"no such file or folder: {src}")


def do_encode_text(text: str, out: Path | None, compress: bool, force: bool) -> None:
    _write_png(io.BytesIO(text.encode("utf-8")), TEXT_NAME, out or Path("text.png"), compress, force,
               f"text ({len(text):,} chars)", FLAG_TEXT)


def do_decode(src: Path, out_dir: Path | None, force: bool) -> None:
    """Text payloads are printed (raw if piped) unless an output dir is given."""
    out, files, data = None, 0, b""
    with console.status(f"Decoding {escape(src.name)}…"):
        meta, chunks = decode(open_image(src))
        if meta.is_text and out_dir is None:
            data = b"".join(chunks)
        else:
            base = out_dir or src.parent
            out = base / safe_name(meta.name)
            if out.exists() and not force:
                raise ImageStoreError(f"{out} already exists (use -f to overwrite; folders are merged into)")
            if out.exists() and out.is_dir() != meta.is_dir:
                raise ImageStoreError(f"{out} exists as a different type (file vs folder)")
            base.mkdir(parents=True, exist_ok=True)
            if meta.is_dir:
                files = unpack_dir(chunks, base)
            else:
                write_file(chunks, out)

    if out is None:
        text = data.decode("utf-8", errors="replace")
        if sys.stdout.isatty():
            console.print(Panel(Text(text), title="Decoded text", border_style="green", expand=False))
        else:
            sys.stdout.write(text)
        return
    detail = f"{files:,} files, {human(meta.orig_size)} archived" if meta.is_dir else human(meta.orig_size)
    report("Decoded", [
        ("Image", escape(str(src))),
        ("Restored", f"{escape(str(out))}{'/' if meta.is_dir else ''}  ({detail})"),
        ("Integrity", "SHA-256 OK"),
    ])


def do_info(src: Path) -> None:
    img = open_image(src)
    meta, chunks = decode(img)
    try:
        for _ in chunks:
            pass
        integrity, style = "[green]OK[/]", "cyan"
    except ImageStoreError as e:
        integrity, style = f"[red]FAILED[/] ({escape(str(e))})", "red"
    w, h = img.size
    report("ImageStore info", [
        ("Image", f"{escape(str(src))}  ({w}×{h} RGBA)"),
        ("Type", "text string" if meta.is_text else f"{'folder' if meta.is_dir else 'file'}: {escape(meta.name)}"),
        ("Original size", human(meta.orig_size)),
        ("Stored size", f"{human(meta.stored_size)} ({'zlib' if meta.compressed else 'raw'})"),
        ("Capacity", f"{human(w * h * BPP)} across {w * h:,} pixels"),
        ("SHA-256", meta.digest.hex()),
        ("Integrity", integrity),
    ], style)


def clean_path(text: str) -> Path:
    """Tolerate drag-and-drop paths from the terminal (quotes, backslash-escaped spaces)."""
    text = text.strip().strip("'\"").replace("\\ ", " ")
    return Path(text).expanduser()


BROWSE_ROWS = 40


def browse(start: Path, images_only: bool = False, allow_dir: bool = False) -> Path:
    """Tiny file browser: pick by number, '..' to go up, or type/drop any path.

    With allow_dir, '.' picks the folder currently shown.
    """
    cwd = start.resolve()
    while True:
        try:
            entries = [p for p in cwd.iterdir() if not p.name.startswith(".")]
        except OSError as e:
            console.print(f"[red]{escape(str(e))}[/]")
            cwd = cwd.parent
            continue
        if images_only:
            entries = [p for p in entries if p.is_dir() or p.suffix.lower() == ".png"]
        entries.sort(key=lambda p: (not p.is_dir(), p.name.lower()))
        shown = entries[:BROWSE_ROWS]

        table = Table(title=escape(str(cwd)), title_justify="left", header_style="bold",
                      box=None, pad_edge=False)
        table.add_column("#", justify="right", style="dim")
        table.add_column("Name")
        table.add_column("Size", justify="right")
        for i, p in enumerate(shown, 1):
            if p.is_dir():
                table.add_row(str(i), f"[bold blue]{escape(p.name)}/[/]", "")
            else:
                try:
                    size = human(p.stat().st_size)
                except OSError:
                    size = "?"
                table.add_row(str(i), escape(p.name), size)
        console.print(table)
        if len(entries) > len(shown):
            console.print(f"[dim]… {len(entries) - len(shown)} more; type a path to reach them[/]")

        hint = "number, .. = up, . = this folder, or a path" if allow_dir else "number, .. = up, or a path"
        answer = Prompt.ask(f"[bold]Pick[/] [dim]({hint})[/]").strip()
        if not answer:
            continue
        if answer == "." and allow_dir:
            return cwd
        if answer == "..":
            cwd = cwd.parent
            continue
        if answer.isdigit() and 1 <= int(answer) <= len(shown):
            chosen = shown[int(answer) - 1]
        else:
            chosen = clean_path(answer)
            if not chosen.is_absolute():
                chosen = cwd / chosen
        if chosen.is_dir():
            cwd = chosen.resolve()
            continue
        return chosen


def interactive() -> None:
    console.print(Panel.fit(
        "[bold cyan]ImageStore[/]\n[dim]hide files in RGBA PNGs - R, G, B and alpha all carry data[/]",
        border_style="cyan",
    ))
    here = Path.cwd()
    while True:
        console.print("\n  [bold]e[/] encode file/folder   [bold]t[/] encode text   "
                      "[bold]d[/] decode   [bold]i[/] info   [bold]q[/] quit")
        choice = Prompt.ask("Action", choices=["e", "t", "d", "i", "q"], default="e", show_choices=False)
        if choice == "q":
            return
        try:
            if choice == "e":
                src = browse(here, allow_dir=True)
                here = src.parent
                out = clean_path(Prompt.ask("Output PNG", default=str(default_png_path(src))))
                compress = Confirm.ask("Compress with zlib first?", default=True)
                force = out.exists() and Confirm.ask(f"{escape(str(out))} exists. Overwrite?", default=False)
                do_encode(src, out, compress, force)
            elif choice == "t":
                console.print("[dim]Enter text, finish with a blank line:[/]")
                lines = []
                while (line := input()) != "":
                    lines.append(line)
                text = "\n".join(lines)
                if not text:
                    raise ImageStoreError("no text entered")
                out = clean_path(Prompt.ask("Output PNG", default="text.png"))
                compress = Confirm.ask("Compress with zlib first?", default=True)
                force = out.exists() and Confirm.ask(f"{escape(str(out))} exists. Overwrite?", default=False)
                do_encode_text(text, out, compress, force)
            elif choice == "d":
                src = browse(here, images_only=True)
                here = src.parent
                meta, _ = decode(open_image(src))
                target = src.parent / safe_name(meta.name)
                force = not meta.is_text and target.exists() and Confirm.ask(f"{escape(str(target))} exists. Overwrite?", default=False)
                do_decode(src, None, force)
            else:
                src = browse(here, images_only=True)
                here = src.parent
                do_info(src)
        except ImageStoreError as e:
            console.print(f"[bold red]error:[/] {escape(str(e))}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="imagestore", description="Serialize files into RGBA PNG images.")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("encode", help="file -> PNG")
    p.add_argument("file", type=Path, nargs="?", help="file or folder to encode (or use -t)")
    p.add_argument("-t", "--text", help="encode this string instead of a file")
    p.add_argument("-o", "--output", type=Path, help="output PNG (default: <file>.png, or text.png)")
    p.add_argument("--no-compress", action="store_true", help="skip zlib pre-compression")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--auto-ignore", action="store_true",
                   help="folders: skip .DS_Store, node_modules, __pycache__, .git, ... (default: ask)")
    g.add_argument("--no-auto-ignore", action="store_true", help="folders: include everything, don't ask")

    p = sub.add_parser("decode", help="PNG -> original file")
    p.add_argument("image", type=Path)
    p.add_argument("-o", "--output-dir", type=Path, help="directory to restore into (default: next to image)")
    p.add_argument("-f", "--force", action="store_true", help="overwrite existing output")

    p = sub.add_parser("info", help="show metadata and verify integrity")
    p.add_argument("image", type=Path)

    args = ap.parse_args()
    try:
        if args.cmd == "encode":
            if (args.file is None) == (args.text is None):
                ap.error("encode needs exactly one of FILE or -t TEXT")
            if args.text is not None:
                do_encode_text(args.text, args.output, not args.no_compress, args.force)
            else:
                # ask only when someone is there to answer; scripts get exact, unfiltered folders
                ignore = True if args.auto_ignore else False if args.no_auto_ignore else (
                    None if sys.stdin.isatty() else False)
                do_encode(args.file, args.output, not args.no_compress, args.force, ignore)
        elif args.cmd == "decode":
            do_decode(args.image, args.output_dir, args.force)
        elif args.cmd == "info":
            do_info(args.image)
        elif sys.stdin.isatty():
            interactive()
        else:
            ap.print_help()
    except ImageStoreError as e:
        console.print(f"[bold red]error:[/] {escape(str(e))}")
        return 1
    except (KeyboardInterrupt, EOFError):
        console.print()
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
