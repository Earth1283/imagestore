# eXact (eXtensible Accurate Color Toolkit)

> **Edit what you intend. Preserve what you don't.**

eXact is a binary-preserving pixel editor for PNG images. It is built for images whose
pixels *are data*: for example, [ImageStore](#background) PNGs, which store arbitrary files
in their RGBA channels together with binary headers and SHA-256 checks.

General-purpose editors such as macOS Preview can change pixels you never touched when
they save: they rewrite the RGB of transparent pixels, premultiply alpha, or apply
colour profiles. The image still *looks* the same, but every payload byte that changed
is now corrupt. eXact's core guarantee is that **every channel byte you did not
explicitly edit stays exactly as it was**, and every save proves it before your file is
touched.

eXact is not primarily a photo editor. Visual correctness is not enough; pixel
correctness is mandatory.

---

## Contents

- [Installation](#installation)
- [Launching](#launching)
- [Using the editor](#using-the-editor)
- [Preservation guarantees](#preservation-guarantees)
- [How it works](#how-it-works)
- [Command-line tools](#command-line-tools)
- [Tests](#tests)
- [Performance (measured)](#performance-measured)
- [Limitations and unimplemented features](#limitations-and-unimplemented-features)
- [Project structure](#project-structure)

---

## Installation

You need Python **3.11 or newer**. eXact depends on PyQt6, Pillow and NumPy; pytest and
psutil are optional development dependencies.

```bash
git clone <this repo> eXact && cd eXact
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -e '.[dev]'              # app + pytest/psutil
```

You can also install with `pip install -r requirements.txt` and run from the source tree
with `PYTHONPATH=src`.

## Launching

```bash
exact                       # GUI
exact path/to/image.png     # GUI, opening a file
python -m exact             # same, without the entry-point script
exact-cli --help            # command-line tools (see below)
```

You can also drag a PNG onto the window to open it.

---

## Using the editor

### Window layout

| Area | What it shows |
|---|---|
| **Canvas** (centre) | Nearest-neighbour view, from 12.5% to 3200%, with an optional pixel grid at 400% and above. |
| **Pixel Inspector** (right) | The inspected pixel's coordinates, canonical byte offset, raw bytes, and each channel in decimal, hex and binary. You can type exact values (or `RRGGBBAA`) and **Apply to pixel**. |
| **Integrity** (right) | Dimensions, mode, bytes per pixel, the current / original / last-saved pixel SHA-256, modified pixel and byte counts, changed-pixel percentage, net transform, whether dimensions changed, undo memory, and the last operation's exact changed-pixel count and bounding box. |
| **Hex Bytes** (bottom) | Hex and ASCII dump of the canonical buffer around the inspected pixel, with that pixel's bytes highlighted. You can jump to any byte offset (`1234` or `0x4D2`) or let it follow the cursor. Payload headers such as `IMGSTORE` show up in the ASCII column. |
| **Image Info** (bottom tab) | The source file's IHDR, interlacing, a list of every chunk, tEXt entries, and load warnings (ICC/gAMA present but not applied, APNG, CRC errors, and so on). |

The toolbar also has a **display mode** selector, a **before/after** selector, and a split
slider:

- **Display modes:** composite over checkerboard, RGB ignoring alpha (reveals data hidden
  in transparent pixels), alpha as greyscale, and the single R, G or B channel.
- **Before/after:** Current, Original, or Split (original on the left, current on the
  right).

**View → Highlight Changed Pixels** tints every pixel whose value differs from the
original. All of these are display-only.

### Tools and shortcuts

On macOS, *Ctrl* below means *⌘*.

| Action | Shortcut |
|---|---|
| Inspect / Pencil / Rectangle select / Hand tool | `I` / `P` / `M` / `H` |
| Pan | Space + drag, middle-drag, Hand tool, or scroll |
| Zoom in / out / 100% / fit | Ctrl + `=` / Ctrl + `-` / Ctrl + `0` / Ctrl + `9`, or Ctrl + scroll |
| Pixel grid | Ctrl + `'` |
| Show original (before) | `\` |
| Highlight changed pixels | Ctrl + Shift + `H` |
| Pick pencil colour from image | Alt + click (Pencil tool) |
| Go to pixel… | Ctrl + `G` |
| Copy selection / Paste (click to place) / Paste at coordinates | Ctrl + `C` / Ctrl + `V` / Ctrl + Shift + `V` |
| Cancel paste placement | `Esc` or right-click |
| Fill selection with pencil colour | Ctrl + Backspace |
| Select all / Deselect | Ctrl + `A` / Ctrl + `D` |
| Rotate 90° CW / CCW | Ctrl + `R` / Ctrl + Shift + `R` (180° and flips are in the **Image** menu) |
| Undo / Redo | Ctrl + `Z` / Ctrl + Shift + `Z` |
| Compare two PNGs | Ctrl + Shift + `D` |

**Pencil.** Set the colour in the tool bar with exact `RRGGBBAA` hex values, the colour
dialog, Alt+click, or *Use as pencil colour* in the inspector. Sizes range from 1 to 64
pixels (square, no anti-aliasing). The **Write: R G B A** checkboxes are a channel write
mask: unchecked channels are never modified. For example, you can rewrite only the alpha
channel of a region.

**Paste** writes raw channel values. If the clipboard and the image have different
channel counts, eXact explains what will happen and asks first:

- RGBA into RGB: the clipboard's alpha is dropped.
- RGB into RGBA: R, G and B are written and the destination alpha is left untouched.

The clipboard is internal to eXact, so you can copy between images you open in turn. It
is deliberately not the system clipboard, because other applications may convert the data.

**Rotations and flips** show a one-time notice. The transform preserves every value, but
pixel-order payloads will not decode until you apply the inverse.

### Pixel-difference viewer

**File → Compare PNG Pixel Buffers…** compares two PNG files, or the current unsaved
buffer against a file. The two images must have equal dimensions and mode. The report
shows:

- whether the PNG files are byte-identical, whether the decoded pixel buffers are
  identical, and whether the images are visually identical
- both pixel SHA-256 hashes
- exact match percentage, changed pixel count and changed channel-byte count
- the first differing coordinate, with both RGBA values
- the bounding box of the changed region
- a count of *invisible* changes: pixels that differ only in the RGB of fully transparent
  pixels

The heatmap is a zoomable view with the bounding box outlined. Dark means identical, and
changes run from red (difference of 1) to yellow (255). When zoomed out, each screen
pixel shows the *maximum* of the pixels it covers, so a single changed pixel never
disappears. Hovering shows A and B values side by side. You can also save the heatmap as
a PNG.

---

## Preservation guarantees

eXact keeps three notions of "the same" separate and never conflates them:

| Level | Meaning | How eXact measures it |
|---|---|---|
| **Byte-identical PNG files** | The same file bytes | SHA-256 of the file |
| **Identical decoded pixel buffers** | Every channel byte equal | SHA-256 and byte comparison of canonical bytes |
| **Visually identical** | Equal after compositing over any background | Differs from the row above only when the RGB of fully transparent pixels changed, which is exactly the damage lossy editors do |

### Guaranteed

1. **Untouched pixels keep their exact R, G, B and A bytes.** All edits *replace* values in
   the pixels you target. eXact never blends, composites, premultiplies, colour-manages,
   gamma-corrects, resizes or normalises. Writing into a transparent pixel changes only
   that pixel's selected channels and nothing around it.
2. **No silent mode conversion.** RGB stays RGB and RGBA stays RGBA. eXact never adds or
   drops an alpha channel.
3. **Transforms are pure permutations.** Rotate 90° CW/CCW, rotate 180° and both flips
   move whole pixels; the inverse transform restores the original buffer byte for byte.
   Tests cover all three-step sequences of these transforms.
4. **Every save is verified before it can do harm.** eXact writes the file to a temporary
   file in the destination directory. It then re-parses that file, decodes it with an
   independent decoder (Pillow), and compares the result byte for byte with the intended
   buffer. Only after that does it atomically replace the destination with `os.replace`.
   If anything fails, the original file is not modified and the temporary file is
   removed.
5. **Exact accounting.** Each operation reports the exact number of pixels and channel
   bytes it changed and their bounding box. The integrity counters are maintained
   incrementally, and tests check them against full rescans.
6. **Unsupported formats are refused, not converted.** This covers 16-bit, greyscale,
   greyscale+alpha and palette PNGs. eXact checks IHDR *before* decoding, because Pillow
   would otherwise silently truncate 16-bit RGB to 8-bit.

### Not guaranteed

- **Byte-identical PNG files.** eXact re-compresses on save, so file size and file SHA-256
  normally change even when every pixel byte is identical. Saving an unchanged document
  over its own file is skipped entirely ("Nothing to save").
- **Decodability of a payload after a rotation or flip.** The values are intact but in a
  different order. Apply the inverse transform before decoding.
- **Some file data** is listed before saving and needs your confirmation; see the
  metadata policy below.

### Metadata policy on save

| Chunk(s) | Action |
|---|---|
| IHDR, IDAT, IEND | Regenerated from the buffer: 8-bit, original colour type, non-interlaced |
| tEXt, zTXt, iTXt, iCCP, sRGB, gAMA, cHRM, cICP, mDCV, cLLI, sBIT, bKGD, tRNS, hIST, sPLT, eXIf, PLTE (suggested palette) | Preserved byte for byte, in their original position before or after IDAT |
| pHYs | Preserved; X and Y density are swapped after a 90° rotation |
| tIME | Preserved if the pixels are unchanged, otherwise set to now (UTC) |
| eXIf after a 90° rotation | Preserved, but EXIF orientation and dimension tags are **not** rewritten |
| Unknown safe-to-copy chunks (lowercase 4th letter) | Preserved |
| Unknown unsafe-to-copy chunks | Preserved only if the pixel data is unchanged; otherwise dropped, as the PNG specification requires |
| APNG (acTL / fcTL / fdAT), dSIG, bytes after IEND | Dropped. eXact lists them and asks for confirmation before saving |
| Adam7 interlacing | Not preserved; output is non-interlaced. Pixel values are unaffected |

Colour-interpretation chunks (iCCP, gAMA and the like) are **never applied**: the canvas
always shows raw channel values.

---

## How it works

### Canonical pixel buffer

The only authoritative image data is a NumPy `uint8` array of shape `(height, width,
channels)`:

- C-contiguous and row-major, with row 0 at the top
- channel order `R, G, B` for RGB images and `R, G, B, A` for RGBA
- straight (non-premultiplied) alpha, exactly as stored in the PNG

The byte offset of pixel `(x, y)`, channel `c`, is `(y * width + x) * channels + c`. All
pixel hashes are SHA-256 over these canonical bytes. They are not computed over the PNG
file, and dimensions and mode are reported separately rather than hashed.

eXact also keeps an immutable copy of the buffer as loaded. Before/after views and the
"modified" counters compare against that copy, mapped through the net rotate/flip, which
eXact tracks as an element of the dihedral group D4. This means "modified pixels" counts
value edits only, while "differs at same (x, y)" is the plain positional comparison.

### Loading

1. eXact parses the PNG chunk structure itself, validating every CRC and hashing the file
   bytes.
2. It accepts only 8-bit colour type 2 (RGB) or 6 (RGBA).
3. Pillow decodes the image, which eXact copies in horizontal strips straight into the
   canonical buffer.
4. Pillow's block size is raised so that its decode buffer is one allocation. macOS
   returns it to the OS when it is freed; with Pillow's default 16 MiB blocks, about
   225 MB stayed resident.

### Display: viewport tiling

Qt never holds authoritative pixels:

- The canvas renders **256×256 display tiles**. Each tile is a throwaway, opaque RGB array
  produced by slicing the visible region of the buffer, so Qt never sees alpha and
  premultiplied formats can never touch real pixels.
- Tiles are drawn with nearest-neighbour scaling and kept in a 96 MB LRU cache, keyed by
  the display settings (display mode, before/after mode, highlight, checker size and zoom
  step).
- When pixels change, the Document notifies listeners with the **dirty rectangle**. Only
  tiles that intersect it are discarded, while scrolling and hover repaints become blits
  of cached tiles.

### Editing and undo

- Commands store only what changed:
  - **Sparse patches:** coordinates plus before/after values, for pencil strokes and
    single-pixel edits.
  - **Rectangle patches:** before/after copies, for fills and pastes.
  - **Transforms:** the operation name only, since undo applies the exact inverse.
- No operation ever snapshots the whole image.
- History has a 1 GiB budget, after which the oldest steps are dropped.

### Saving: dirty row-bands

eXact writes IDAT itself:

- **Bands.** The image is split into row-bands of about 4 MiB. Each band gets adaptive
  PNG filtering (filter type chosen per row by the specification's minimum-sum heuristic,
  scored on sampled column spans) and is deflated in parallel threads.
- **Stitching.** Each band is an independent raw-deflate segment that ends in a sync
  flush. The segments concatenate into one valid zlib stream, and their Adler-32 values
  are merged with a port of zlib's `adler32_combine`.
- **Dirty-band reuse.** The Document records which **rows have been dirtied** since the
  last save. On the next save, bands whose rows (and the row above them) are clean are
  copied verbatim from the previously written file, which acts as an on-disk cache and
  costs no extra RAM. Only dirty bands are re-encoded. A test asserts that the result is
  byte-identical to a full re-encode.
- **Safety net.** The cache is used only if the file's inode, size and mtime still match.
  The full decode verification still runs on every save, and if a reused band ever failed
  verification, eXact retries with a full encode.

---

## Command-line tools

```bash
exact-cli info image.png                  # IHDR, chunks, warnings, pixel and file SHA-256
exact-cli hash image.png                  # pixel SHA-256 and file SHA-256
exact-cli compare a.png b.png [--heatmap diff.png]   # exit 0 identical, 1 differ, 2 incomparable
exact-cli roundtrip image.png             # verified save to a temp file, then compare
exact-cli make-test-image out.png --size 7488x7487 --mode RGBA --seed 921
```

`make-test-image` writes a deterministic image that resembles a binary payload: random
bytes in every channel, an `IMGSTORE` header holding the SHA-256 of the body, a band of
alpha=0 pixels with non-zero RGB, and so on.

---

## Tests

```bash
python -m pytest            # 68 tests, about 20 s
python -m pytest --runslow  # adds 7488×7487 full-size tests (70 total)
```

The tests cover:

- RGBA and RGB round trips
- load → save without edits producing identical raw bytes
- transparent pixels with non-zero RGB
- arbitrary binary data in all four channels
- alpha preservation with no premultiplication
- refusal of 16-bit, greyscale, grey+alpha and palette images
- one-pixel edits that change exactly one pixel, and preservation of untouched pixels
  (including next to transparent pixels)
- pencil lines touching only line pixels, and the channel write mask
- undo/redo accuracy across mixed operations
- incremental counters checked against full rescans over 60 random operations
- every transform and its inverse, D4 composition, and pure-permutation checks
- failed-save protection: injected verification failure and injected I/O error both
  leave the original byte-identical with no temporary file left behind
- permission preservation and SHA-256 over canonical bytes
- metadata preservation and dropping rules, including the pHYs swap
- RGB+tRNS and Adam7-interlaced sources
- dirty-band reuse: byte-identical to a full encode, correct invalidation across band
  boundaries, after undo and after transforms, corrupt-cache fallback, and a cache
  ignored when the file changed on disk
- the diff engine
- a headless GUI suite on the Qt offscreen platform: real mouse events drive the pencil,
  transforms run through the window, saved output is checked, the inspector and hex
  panel are exercised, and screen pixels are checked after edit, undo and transform to
  confirm tile invalidation
- the **final acceptance test**

**Final acceptance test** (`tests/test_acceptance.py`):

1. Create an image with arbitrary binary RGBA values and open it.
2. Rotate 90° CW, save, and reopen.
3. Rotate 90° CCW and save again.
4. Check that the final buffer equals the initial one by both SHA-256 and direct byte
   comparison.
5. Edit one pixel, save, and check that exactly one pixel differs.

The suite passes on Python 3.14.2 and 3.11.

---

## Performance (measured)

Measured with `python scripts/benchmark.py`, using a 7488×7487 RGBA PNG of random bytes
(224 MB raw, 224 MB as PNG, since random data does not compress). Each scenario runs in a
fresh process, and peak RSS is `ru_maxrss` for that process.

**Machine:** MacBook with Intel Core i5-1030NG7 (1.1 GHz, 4 cores), 16 GB RAM, macOS
15.8.2, Python 3.14.2, PyQt6 6.10.1, Pillow 12.1.0, NumPy 2.4.2. This CPU throttles
heavily, so times are given as the range over three uncontended runs. A fourth run that
overlapped a `pip install` was discarded.

| Scenario | Peak RSS | Time |
|---|---|---|
| Open (decode + verify structure + baseline copy + hash) | 507 MB | 1.7–3.0 s |
| Rotate 90° CW, then CCW (hash checked equal) | 717 MB | 0.8–3.0 s per rotation |
| Full verified save (encode + fsync + full decode + byte compare + atomic replace) | 756 MB | 7.0–9.4 s |
| Verified save after a 1-pixel edit (53 of 54 bands reused) | 756 MB | 3.2–5.0 s |
| Pencil stroke, 21,990 pixels (size 4) | – | 0.11–0.26 s |
| SHA-256 of the pixel buffer | – | 0.21–0.50 s |
| GUI: open + paint a 1600×1000 window | 581 MB | – |
| Paint: cold tiles / cached / after a 64 px scroll | – | 63–110 ms / 3–15 ms / 2–36 ms |

**Expected memory:** the buffer (224 MB) plus the immutable original (224 MB) plus about
60 MB of runtime. Rotation briefly needs a second buffer, and save verification needs
the decoder's copy.

**Before the tiling and dirty-band work**, the same full save took 45 s, opening peaked at
706 MB and rotation at 930 MB.

---

## Limitations and unimplemented features

These are not implemented in v0.1.0:

- **Formats:** only 8-bit RGB and 8-bit RGBA PNGs. 16-bit, greyscale, palette, JPEG and
  other formats are refused with an explanation.
- **APNG:** only the default image is edited; animation frames are dropped on save after
  confirmation.
- **Annotation tools:** there are no line, shape or text tools. You can draw with the
  pencil (with a channel mask), fill a rectangle, paste, and set exact values for one
  pixel.
- **Selections:** a selection cannot be moved by dragging (paste is placed by click or by
  coordinates). There is no crop or resize, since those change dimensions.
- **System clipboard:** eXact uses an internal clipboard only, by design.
- **One document per window.**
- **EXIF:** orientation and dimension tags are not rewritten after rotation.
- **File-system metadata:** an atomic replace creates a new inode, so macOS extended
  attributes, Finder tags and hard links to the old file are not carried over. POSIX
  permission bits are copied. Symlinks are resolved and their target is replaced.
- **Colour-managed display:** not provided, intentionally. The canvas shows raw values.
- **Zoomed-out display:** below 100% the editor canvas shows sampled pixels, so a single
  edited pixel may not be visible. The difference heatmap max-pools instead.
- **Platforms:** developed and tested on macOS only. The code uses only portable APIs
  (`os.replace`, Qt, NumPy and Pillow; the directory fsync is skipped on Windows), but it
  has **not been run on Windows or Linux**.
- **GUI testing:** the GUI is tested headlessly (offscreen Qt with synthetic mouse events)
  and was checked visually from offscreen screenshots. It has not been exercised by hand
  on a physical display.

---

## Project structure

```
eXact/
├── pyproject.toml / requirements.txt
├── src/exact/
│   ├── __main__.py           # python -m exact
│   ├── cli.py                # exact-cli
│   ├── core/                 # Qt-free, fully unit-tested
│   │   ├── pixels.py         # canonical buffer conventions, SHA-256
│   │   ├── png_chunks.py     # chunk parsing/writing, CRC, IHDR
│   │   ├── png_io.py         # strict load, banded encoder, verified atomic save
│   │   ├── transforms.py     # rotations/flips, D4 orientation tracking
│   │   ├── history.py        # patch-based undo/redo
│   │   ├── document.py       # the editable document, dirty tracking
│   │   ├── diff.py           # raw buffer / file comparison
│   │   └── synthetic.py      # deterministic payload-like test images
│   └── gui/                  # PyQt6
│       ├── app.py            # entry point
│       ├── main_window.py    # menus, actions, save/open flows
│       ├── canvas.py         # tiled zoom/pan view, tools, tile cache
│       ├── display.py        # display-only conversions (composite, channels, heatmap)
│       ├── panels.py         # inspector, hex, info, integrity, tool options
│       ├── diff_dialog.py    # pixel-difference viewer
│       └── tasks.py          # background work behind a modal progress dialog
├── tests/                    # pytest suite (see above)
└── scripts/benchmark.py      # peak-RSS / timing benchmark
```

---

## Background

ImageStore is a Python application that encodes arbitrary files, folders, source code
and databases into RGBA PNG images, storing the binary data directly in pixel channels.
Any change to a single byte breaks its headers and SHA-256 integrity checks, which is
why eXact exists.

**Official motto:** *Code is art. Handle with care.*

**Unofficial motto:** *Unlike Preview, we don't touch your fucking bytes.*
