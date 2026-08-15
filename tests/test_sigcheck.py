"""sig_extract.py: dotfile-sidecar output naming and collision safety."""

import subprocess
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / ".claude"
    / "skills"
    / "sigcheck"
    / "scripts"
    / "sig_extract.py"
)

SIG_BODY = (
    "Some body text.\n"
    "IN WITNESS WHEREOF, the parties have executed this Agreement.\n"
    "By: Jane Signer\n"
)


def run_extract(*args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
    )


def test_dotfile_input_writes_visible_output(tmp_path):
    src = tmp_path / ".contract.docx.md"
    src.write_text(SIG_BODY, encoding="utf-8")
    proc = run_extract(src, "--outdir", tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = tmp_path / "contract.docx_sigs.md"
    assert out.exists()
    assert "IN WITNESS WHEREOF" in out.read_text(encoding="utf-8")


def test_double_dot_input_still_visible(tmp_path):
    src = tmp_path / "..weird.docx.md"
    src.write_text(SIG_BODY, encoding="utf-8")
    proc = run_extract(src, "--outdir", tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "weird.docx_sigs.md").exists()


def test_colliding_pair_writes_nothing_and_fails(tmp_path):
    plain = tmp_path / "contract.docx.md"
    dotted = tmp_path / ".contract.docx.md"
    plain.write_text(SIG_BODY, encoding="utf-8")
    dotted.write_text(SIG_BODY, encoding="utf-8")
    proc = run_extract(plain, dotted, "--outdir", tmp_path)
    assert proc.returncode == 1
    assert "collision" in proc.stderr
    assert str(plain) in proc.stderr and str(dotted) in proc.stderr
    assert not list(tmp_path.glob("*_sigs.md"))  # nothing written


def test_dot_only_name_fails_that_file_clearly(tmp_path):
    src = tmp_path / "..md"
    src.write_text(SIG_BODY, encoding="utf-8")
    proc = run_extract(src)
    assert proc.returncode == 1
    assert "output name is empty" in proc.stderr
