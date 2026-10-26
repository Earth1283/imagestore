# imagestore

*Turn files into pixels. Recover them exactly.*

A single-file Python CLI that stashes any file, folder or string inside an RGBA PNG. R, G,
B and alpha all carry data, and a SHA-256 digest in the header verifies every decode. An
optional graphical interface sits on top of the same operations.

## Files

| File | Purpose |
|---|---|
| `imagestore.py` | The tool: format, encoder, decoder, verified operations, CLI and interactive menu |
| `imagestore_gui/` | Optional PyQt6 interface (Encode, Decode, Inspect). Never loaded unless asked for |
| `requirements.txt` | Command-line dependencies: `Pillow`, `rich` |
| `requirements-gui.txt` | Adds `PyQt6` for the graphical interface |
| `requirements-dev.txt` | Adds `pytest` for the test suite |
| `tests/` | Test suite; `tests/fixtures/legacy/` holds archives made by the previous encoder |
| `imagestore.py.png` | Sample: `imagestore.py` encoded by itself (re-encode with `python imagestore.py encode imagestore.py -f` after changing the script) |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt        # command line only
pip install -r requirements-gui.txt    # optional: graphical interface
```

## Usage

```bash
python imagestore.py                          # interactive menu (in a terminal)
python imagestore.py -g                       # graphical interface (also --gui)
python imagestore.py encode report.pdf        # -> report.pdf.png
python imagestore.py encode ./my-folder       # folder, tarred first
python imagestore.py encode -t "some text"    # -> text.png
python imagestore.py decode report.pdf.png    # restores report.pdf next to the image
python imagestore.py info report.pdf.png      # metadata + integrity check
```

Run with no arguments in a terminal for the menu: `e` encode, `t` text, `d` decode,
`i` inspect, `g` open the GUI, `h` help, `q` quit. Without a terminal it prints help, so
scripts never block on a prompt. The GUI only opens with `-g`/`--gui` or `g`; the command
line never imports PyQt6 otherwise.

In the menu, folders that contain junk (`.DS_Store`, `node_modules`, `__pycache__`, `.git`,
`.venv`, ...) offer `y` skip, `n` keep everything, or `r` review: a numbered list where you
pick the entries to keep. Ctrl+C during an operation cancels it and returns to the menu;
partial output is removed.

### Graphical interface

`python imagestore.py -g` (or `python -m imagestore_gui` from this folder) opens a window
with three pages:

- **Encode**: drop a file or folder (or type text), review auto-ignore decisions entry by
  entry, choose the output, toggle compression and verification. Results show original,
  stored payload and PNG sizes, compression, dimensions, padding and the checksum.
- **Decode**: drop a PNG to see what it contains before restoring it; choose where it goes.
  Text payloads can be shown in the window instead of written to disk.
- **Inspect**: header, capacity and format details, and a full integrity check that shows
  the stored and computed SHA-256 when they differ.

Long operations run in the background with progress and a Cancel button (Esc). Shortcuts:
Ctrl+1/2/3 switch pages, Ctrl+O chooses an input, Ctrl+Enter runs. Dark and light themes are
under View > Theme.

### Flags

- `-g/--gui`: open the graphical interface
- `encode`: `-o/--output PATH`, `-t/--text TEXT`, `--no-compress`, `-f/--force`,
  `--no-verify`, `--auto-ignore` / `--no-auto-ignore` (folders: skip or keep `.DS_Store`,
  `node_modules`, `__pycache__`, `.git`, `.venv` and similar; asks interactively, keeps
  everything when scripted)
- `decode`: `-o/--output-dir DIR`, `-f/--force`

Exit codes: `0` success, `1` error (bad image, failed integrity check, I/O error, etc.),
`130` interrupted. Errors are printed to stderr as a short title, the technical detail and
a hint.

## Guarantees

- The archive format, compression and SHA-256 check are unchanged from format version 2;
  the test suite decodes archives made by the previous encoder and checks that uncompressed
  inputs re-encode to identical pixel buffers.
- An encode is reported as done only after the written PNG has been decoded again and its
  SHA-256 matched. It is written to a temporary file and moved into place, so a failure or
  cancel never leaves a half-written PNG.
- Decoded output is written only after the size and SHA-256 checks pass. Folders are
  extracted into a staging folder and moved into place, so a failed restore leaves nothing.
- File names that are not valid UTF-8 are stored with U+FFFD in place of the undecodable
  bytes; the file content is unaffected.

## Notes

- Keep the PNG pixel-exact. Lossy re-encoding, alpha premultiplication or stripping alpha
  breaks the SHA-256 check; the error message says so. Use [eXact](../eXact/README.md) to
  edit these images safely.
- zlib or LZMA compression (whichever is smaller) is applied only when it makes the payload smaller.
- Error correction (`--ecc`) is off unless asked for; see the main README.
- The digest detects corruption; it is not encryption.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest tests          # 115 tests, a few seconds; GUI tests skip without PyQt6
QT_QPA_PLATFORM=offscreen python -m pytest tests   # headless machines
```

See the [top-level README](../README.md) for the header format.
