"""Command-line behaviour: in-process main() calls, plus two real subprocess runs without a terminal."""
from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "imagestore.py"


@pytest.fixture(autouse=True)
def no_terminal(monkeypatch):
    """In-process main() must never prompt, so make stdin explicitly a non-terminal."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))


def run_script(*args, cwd: Path):
    return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd, stdin=subprocess.DEVNULL,
                          capture_output=True, timeout=120)


def test_encode_file_refuses_overwrite_without_force(istore, tmp_path, capsys):
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hello file\n")

    assert istore.main(["encode", str(src)]) == 0
    assert (tmp_path / "notes.txt.png").exists()

    capsys.readouterr()
    assert istore.main(["encode", str(src)]) == 1
    assert "error:" in capsys.readouterr().err

    assert istore.main(["encode", str(src), "-f"]) == 0


def test_encode_needs_exactly_one_of_file_or_text(istore):
    with pytest.raises(SystemExit) as ei:
        istore.main(["encode"])
    assert ei.value.code == 2


def test_text_encode_then_subprocess_decode_prints_raw_text(istore, tmp_path):
    out = tmp_path / "hello.png"
    assert istore.main(["encode", "-t", "hello", "-o", str(out)]) == 0

    proc = run_script("decode", str(out), cwd=tmp_path)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.decode("utf-8") == "hello"  # no panel, no decoration, no added newline


def test_folder_encode_without_terminal_does_not_prompt_and_keeps_everything(istore, tmp_path, project,
                                                                             regular_files):
    assert istore.main(["encode", str(project.path)]) == 0
    png = tmp_path / "proj.png"
    assert png.exists()

    dest = tmp_path / "restored"
    assert istore.main(["decode", str(png), "-o", str(dest)]) == 0
    assert regular_files(dest / "proj") == project.all


def test_folder_auto_ignore_excludes_junk(istore, tmp_path, project, regular_files):
    out2 = tmp_path / "clean.png"
    assert istore.main(["encode", str(project.path), "--auto-ignore", "-o", str(out2)]) == 0

    dest = tmp_path / "restored"
    assert istore.main(["decode", str(out2), "-o", str(dest)]) == 0
    assert regular_files(dest / "proj") == project.keep


def test_encode_no_verify_writes_readable_archive(istore, tmp_path, rand_bytes, capsys):
    src = tmp_path / "blob.bin"
    src.write_bytes(rand_bytes(700, seed=8))
    out3 = tmp_path / "plain.png"

    assert istore.main(["encode", "--no-verify", str(src), "-o", str(out3)]) == 0
    assert out3.exists()
    assert istore.main(["info", str(out3)]) == 0


def test_info_exit_codes(istore, tmp_path, rand_bytes, tamper):
    src = tmp_path / "blob.bin"
    src.write_bytes(rand_bytes(5000, seed=3))
    good = tmp_path / "blob.png"
    istore.encode_path(src, good, compress=False)
    bad = tamper(good, tmp_path / "bad.png", offset=200)

    assert istore.main(["info", str(good)]) == 0
    assert istore.main(["info", str(bad)]) == 1


def test_decode_missing_file_prints_error_not_traceback(istore, tmp_path, capsys):
    rc = istore.main(["decode", str(tmp_path / "missing.png")])

    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "Not found" in err
    assert "Traceback" not in err


def test_gui_flag_cannot_be_combined_with_a_command(istore):
    with pytest.raises(SystemExit) as ei:
        istore.main(["-g", "encode", "x"])
    assert ei.value.code == 2


def test_no_arguments_without_terminal_prints_help_not_menu(tmp_path):
    proc = run_script(cwd=tmp_path)

    assert proc.returncode == 0
    assert b"usage" in proc.stdout
    assert b"Action" not in proc.stdout  # the interactive menu's prompt never appears
