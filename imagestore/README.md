# imagestore

A single-file Python CLI that stashes any file, folder or string inside an RGBA PNG. R, G,
B and alpha all carry data, and a SHA-256 digest in the header verifies every decode.

## Files

| File | Purpose |
|---|---|
| `imagestore.py` | The whole tool: encoder, decoder, CLI and interactive menu |
| `requirements.txt` | Dependencies: `Pillow`, `rich` |
| `imagestore.py.png` | Sample: `imagestore.py` encoded by itself (re-encode with `python imagestore.py encode imagestore.py -f` after changing the script) |

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
python imagestore.py                          # interactive menu
python imagestore.py encode report.pdf        # -> report.pdf.png
python imagestore.py encode ./my-folder       # folder, tarred first
python imagestore.py encode -t "some text"    # -> text.png
python imagestore.py decode report.pdf.png    # restores report.pdf next to the image
python imagestore.py info report.pdf.png      # metadata + integrity check
```

Run with no arguments in a terminal for the menu (`e` encode, `t` text, `d` decode,
`i` info, `q` quit). Without a terminal it prints help.

### Flags

- `encode`: `-o/--output PATH`, `-t/--text TEXT`, `--no-compress`, `-f/--force`,
  `--auto-ignore` / `--no-auto-ignore` (folders: skip or keep `.DS_Store`, `node_modules`,
  `__pycache__`, `.git`, `.venv` and similar; asks interactively, keeps everything when
  scripted)
- `decode`: `-o/--output-dir DIR`, `-f/--force`

Exit codes: `0` success, `1` error (bad image, integrity failure, etc.), `130` interrupted.

## Notes

- Keep the PNG pixel-exact. Lossy re-encoding, alpha premultiplication or stripping alpha
  breaks the SHA-256 check; the error message says so. Use [eXact](../eXact/README.md) to
  edit these images safely.
- zlib compression is applied only when it makes the payload smaller.
- Decoded output is written only after the size and SHA-256 checks pass.
- The digest detects corruption; it is not encryption.

See the [top-level README](../README.md) for the header format.
