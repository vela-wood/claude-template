#!/usr/bin/env python3
"""Repo wrapper around the adeu CLI.

Pass-through for every subcommand, plus for `apply`:

  - default track-changes author (ADEU_AUTHOR, else DEFAULT_AUTHOR)
  - underscore blanks in JSON `new_text` encoded so adeu's Markdown parser
    cannot eat them, then restored in the output XML
  - wholly deleted paragraphs get their paragraph mark deleted too

    edits.json --encode--> edits.encoded.json --adeu--> out.raw.docx --post--> out.docx

`--raw` disables the encode/post-process step.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_AUTHOR = "Vela Wood"
AUTHOR_ENV = "ADEU_AUTHOR"
RAW_FLAG = "--raw"
OUTPUT_FLAGS = ("-o", "--output")
ENCODED_SUFFIX = ".encoded.json"
RAW_DOCX_SUFFIX = ".raw.docx"


def _repo_root_from_script() -> Path:
    script_path = Path(__file__).resolve()
    # .../<repo>/.claude/skills/redline/scripts/run_redline.py
    return script_path.parents[4]


def _ensure_repo_imports(repo_root: Path) -> None:
    for p in (repo_root, Path(__file__).resolve().parent):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _find_adeu(repo_root: Path) -> Path | None:
    for candidate in (repo_root / ".venv" / "bin" / "adeu", repo_root / ".venv" / "Scripts" / "adeu.exe"):
        if candidate.exists():
            return candidate
    return None


def _option_value(argv: list[str], flags: tuple[str, ...]) -> str | None:
    for i, a in enumerate(argv):
        if a in flags and i + 1 < len(argv):
            return argv[i + 1]
        for f in flags:
            if a.startswith(f + "="):
                return a[len(f) + 1 :]
    return None


def _replace_option(argv: list[str], flags: tuple[str, ...], value: str) -> list[str]:
    out, skip = [], False
    for a in argv:
        if skip:
            out.append(value); skip = False; continue
        if a in flags:
            out.append(a); skip = True; continue
        if any(a.startswith(f + "=") for f in flags):
            out.append(a.split("=", 1)[0] + "=" + value); continue
        out.append(a)
    return out


def _positionals(argv: list[str]) -> list[str]:
    """Positional args of `apply`, ignoring option flags and their values."""
    valued = set(OUTPUT_FLAGS) | {"--author", "--report"}
    out, skip = [], False
    for a in argv:
        if skip:
            skip = False; continue
        if a in valued:
            skip = True; continue
        if a.startswith("-"):
            continue
        out.append(a)
    return out


def _load_json_edits(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, list) else None


def _encode_edits(changes: Path) -> Path | None:
    """Write a copy of a JSON batch with blanks in `new_text` encoded."""
    from postprocess_docx import BLANK_RUN, encode_blanks

    edits = _load_json_edits(changes)
    if edits is None:
        return None
    touched = False
    for e in edits:
        text = e.get("new_text") if isinstance(e, dict) else None
        if isinstance(text, str) and BLANK_RUN.search(text):
            e["new_text"] = encode_blanks(text)
            touched = True
    if not touched:
        return None

    encoded = changes.with_name(changes.stem + ENCODED_SUFFIX)
    encoded.write_text(json.dumps(edits, indent=1, ensure_ascii=False), encoding="utf-8")
    return encoded


def _run_apply(adeu_bin: Path, argv: list[str], repo_root: Path) -> int:
    from postprocess_docx import postprocess

    raw_mode = RAW_FLAG in argv
    argv = [a for a in argv if a != RAW_FLAG]

    env = os.environ.copy()
    if _option_value(argv, ("--author",)) is None and not env.get(AUTHOR_ENV):
        env[AUTHOR_ENV] = DEFAULT_AUTHOR

    output = _option_value(argv, OUTPUT_FLAGS)
    if raw_mode or output is None:
        if output is None:
            print("run_redline: no -o given; skipping blank/paragraph post-pass", file=sys.stderr)
        return subprocess.call([str(adeu_bin), *argv], cwd=str(repo_root), env=env)

    positionals = _positionals(argv[1:])
    if len(positionals) >= 2:
        encoded = _encode_edits(Path(positionals[1]))
        if encoded is not None:
            argv = [str(encoded) if a == positionals[1] else a for a in argv]
            print(f"run_redline: blanks encoded -> {encoded}", file=sys.stderr)

    out_path = Path(output)
    raw_path = out_path.with_name(out_path.stem + RAW_DOCX_SUFFIX)
    argv = _replace_option(argv, OUTPUT_FLAGS, str(raw_path))

    rc = subprocess.call([str(adeu_bin), *argv], cwd=str(repo_root), env=env)
    if rc != 0 or not raw_path.exists():
        raw_path.unlink(missing_ok=True)
        return rc

    blanks, marks = postprocess(raw_path, out_path)
    raw_path.unlink()
    print(f"run_redline: wrote {out_path} (blanks restored={blanks}, paragraph marks deleted={marks})")
    return 0


def main(argv: list[str]) -> int:
    repo_root = _repo_root_from_script()
    _ensure_repo_imports(repo_root)
    from netdocs.env import load_dotenv_file

    load_dotenv_file(repo_root / ".env", override=True)

    adeu_bin = _find_adeu(repo_root)
    if adeu_bin is None:
        print("adeu binary not found in .venv/bin or .venv/Scripts", file=sys.stderr)
        print(
            "Run 'uv sync' at the repo root. The default 'redline' group installs Adeu into the shared .venv.",
            file=sys.stderr,
        )
        return 1

    if argv and argv[0] == "apply":
        return _run_apply(adeu_bin, argv, repo_root)

    return subprocess.call([str(adeu_bin), *argv], cwd=str(repo_root), env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
