# ImageStore

Stash any file, folder or string inside an RGBA PNG. All four channels carry data (R, G, B
**and alpha**), so the image is a dense, lossless container for arbitrary bytes. Every
payload is protected by a SHA-256 digest, so a decode either returns exactly what you put
in or tells you it was altered.

This repository contains two tools:

| Directory | What it is |
|---|---|
| [`imagestore/`](imagestore/) | The encoder/decoder: a single-file Python CLI with an interactive menu. |
| [`eXact/`](eXact/) | A binary-preserving PNG pixel editor, for editing ImageStore images without corrupting the payload. See its [README](eXact/README.md). |

## Installation

Python 3.8 or newer (developed on 3.14; the code uses the walrus operator and
`from __future__ import annotations`).

```bash
cd imagestore
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt     # Pillow, rich
```

## Usage

```bash
python imagestore.py                          # interactive menu (file browser, prompts)

python imagestore.py encode report.pdf        # -> report.pdf.png
python imagestore.py encode ./my-folder       # folders are tarred first
python imagestore.py encode -t "some text"    # -> text.png
python imagestore.py decode report.pdf.png    # restores report.pdf next to the image
python imagestore.py info report.pdf.png      # metadata + integrity check
```

### Options

| Command | Flag | Effect |
|---|---|---|
| `encode` | `-o, --output PATH` | Output PNG (default `<file>.png`, or `text.png` with `-t`) |
| `encode` | `-t, --text TEXT` | Encode a string instead of a file |
| `encode` | `--no-compress` | Skip zlib pre-compression |
| `encode` | `-f, --force` | Overwrite an existing output |
| `encode` | `--auto-ignore` / `--no-auto-ignore` | Folders: skip (or keep) junk such as `.DS_Store`, `node_modules`, `__pycache__`, `.git`, `.venv`. By default it asks when run interactively and includes everything when scripted. |
| `decode` | `-o, --output-dir DIR` | Restore into `DIR` (default: next to the image) |
| `decode` | `-f, --force` | Overwrite existing output |

Text payloads are printed by `decode` instead of being written to a file. Decoded data is
only committed after the size and SHA-256 checks pass.

## How it works

The image is a header followed by the (optionally zlib-compressed) payload, written
row-major into RGBA bytes. The image is made roughly square, and the unused tail is zero
padding.

```
>4s   magic          "IMST"
B     version        2
B     flags          0x01 zlib, 0x02 text, 0x04 folder (tar)
Q     original size
Q     stored size
32s   SHA-256 of the original bytes
H     name length
...   name (UTF-8)
...   payload
```

Encoding streams the source in 1 MiB chunks and keeps only the pixel buffer in memory.
Compression is used only when it actually makes the payload smaller.

## Caveats

- **The PNG must stay byte-exact at the pixel level.** Anything that re-encodes lossily,
  premultiplies alpha or strips the alpha channel breaks the digest. Many chat apps,
  image hosts and editors (macOS Preview included) do exactly that. If you need to edit
  an ImageStore PNG, use [eXact](eXact/README.md).
- The SHA-256 detects corruption; it is **not encryption**. Anyone with the PNG can decode
  it.
- The format is at version 2. Version 1 images (CRC32) are not readable.

## Sample

[`imagestore/imagestore.py.png`](imagestore/imagestore.py.png) is `imagestore.py` encoded
by itself. Check it with:

```bash
python imagestore.py info imagestore.py.png
python imagestore.py decode imagestore.py.png -o restored
```

It will go stale whenever the script changes, so re-encode it with
`python imagestore.py encode imagestore.py -f`.
