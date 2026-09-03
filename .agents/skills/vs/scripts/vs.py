#!/usr/bin/env python3
"""Version Story CLI: compare, merge, and edit documents via the hosted API.

Talks only to vs_client; never prints the API key.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import vs_client as vsc  # noqa: E402
from vs_client import ApiError, JobFailed, JobKind  # noqa: E402

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_NO_KEY = 2

# One suffix per download format. Files are named <stem><suffix>.
FORMAT_SUFFIX = {
    "docx": ".docx",
    "pdf": ".pdf",
    "pdf_changed_pages_only": "_changed.pdf",
    "md": ".md",
    "json": ".json",
    "redline": "_redline.docx",
}
DEFAULT_AUTHOR = "Velawood"

SETUP_MESSAGE = f"""[MISSING] {vsc.ENV_KEY} is not set.

To get a key:
  1. Sign in at https://versionstory.com and open Settings -> Developer.
     (If Developer is not in the menu, ask your org admin for the Developer permission.)
  2. Create a REST API key. It is shown once in a banner with a copy button;
     copy it before dismissing. If lost, create a new one.
  3. Save it, either by pasting it into this chat so the agent can run
       uv run .claude/skills/vs/scripts/vs.py set-key <key>
     or by adding this line to the .env file at the repo root yourself:
       {vsc.ENV_KEY}=vs_live_...
  4. Re-run:  uv run .claude/skills/vs/scripts/vs.py check
"""


def _repo_root() -> Path:
    # .../<repo>/.claude/skills/vs/scripts/vs.py
    return Path(__file__).resolve().parents[4]


def _load_env(repo_root: Path) -> None:
    sys.path.insert(0, str(repo_root))
    from netdocs.env import load_dotenv_file

    load_dotenv_file(repo_root / ".env", override=True)


def _stamp() -> str:
    return date.today().strftime("%Y%m%d")


def _unique(path: Path) -> Path:
    """Append _2, _3, ... until the path is free."""
    if not path.exists():
        return path

    counter = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def _output_path(outdir: Path, stem: str, fmt: str, inputs: list[Path]) -> Path:
    suffix = FORMAT_SUFFIX[fmt]
    # Split stem/extension so _unique() inserts the counter before the extension.
    base = outdir / f"{stem}{suffix}"
    candidate = _unique(base)
    if candidate.resolve() in {p.resolve() for p in inputs}:
        raise ValueError(f"Output would overwrite an input: {candidate}")
    return candidate


def _parse_formats(raw: str, allowed: tuple[str, ...]) -> list[str]:
    formats = [f.strip() for f in raw.split(",") if f.strip()]
    bad = [f for f in formats if f not in allowed]
    if bad:
        raise ValueError(f"Unsupported format(s) {', '.join(bad)}; allowed: {', '.join(allowed)}")
    return formats


def _fetch_all(client: vsc.Client, kind: JobKind, job_id: str, formats: list[str],
               outdir: Path, stem: str, inputs: list[Path], timeout_s: int) -> int:
    downloads = client.wait(kind, job_id, formats, timeout_s=timeout_s)
    outdir.mkdir(parents=True, exist_ok=True)

    for fmt in formats:
        download = next(d for d in downloads if d.format == fmt)
        target = _output_path(outdir, stem, fmt, inputs)
        target.write_bytes(client.download(download.url))
        print(f"Wrote: {target}")
    return EXIT_OK


# -- subcommands -------------------------------------------------------------

def cmd_check(args: argparse.Namespace, key: str | None) -> int:
    if not key:
        print(SETUP_MESSAGE)
        return EXIT_FAIL

    shown = vsc.redact_key(key)
    if not key.startswith(vsc.KEY_PREFIX):
        print(f"[INVALID] {vsc.ENV_KEY}: {shown} does not start with '{vsc.KEY_PREFIX}'. "
              "Re-copy the key from Settings -> Developer and run set-key again.")
        return EXIT_FAIL

    try:
        vsc.Client(key).check_key()
    except ApiError as exc:
        print(f"[INVALID] {vsc.ENV_KEY}: {shown} rejected ({exc.code}).")
        if exc.http_status == vsc.HTTP_UNAUTHORIZED:
            print("Likely a truncated copy or a revoked key. Create a new key at Settings -> Developer.")
        elif exc.code == "API_KEY_ORG_MISMATCH":
            print("The key belongs to a different organization.")
        return EXIT_FAIL

    print(f"[OK] {vsc.ENV_KEY}: {shown}")
    return EXIT_OK


def cmd_set_key(args: argparse.Namespace, _key: str | None) -> int:
    new_key = args.key.strip()
    if not new_key.startswith(vsc.KEY_PREFIX):
        print(f"Refusing to save: key does not start with '{vsc.KEY_PREFIX}'. "
              "Check the copy from Settings -> Developer.", file=sys.stderr)
        return EXIT_FAIL

    if not args.no_verify:
        try:
            vsc.Client(new_key).check_key()
        except ApiError as exc:
            print(f"Key rejected by Version Story ({exc.code}); not saved.", file=sys.stderr)
            return EXIT_FAIL

    from config.env import write_env_file

    result = write_env_file(_repo_root() / ".env", {vsc.ENV_KEY: new_key})
    if result.appended_new_keys:
        outcome = "added"
    elif result.appended_conflicting_keys:
        outcome = "replaced"
    else:
        outcome = "already saved"
    print(f"{vsc.ENV_KEY} {outcome} in .env ({vsc.redact_key(new_key)}).")
    return EXIT_OK


def cmd_compare(args: argparse.Namespace, key: str) -> int:
    formats = _parse_formats(args.format, vsc.COMPARE_FORMATS)
    for path in (args.original, args.modified):
        vsc.validate_input(path)
    if args.original.resolve() == args.modified.resolve():
        raise ValueError("Original and Modified are the same file")

    outdir = args.outdir or args.modified.parent
    stem = args.name or f"{args.modified.stem}_vs_{args.original.stem}_vscompare_{_stamp()}"

    client = vsc.Client(key)
    job_id = client.compare(args.original, args.modified, author=args.author)
    print(f"comparison_id: {job_id}")
    return _fetch_all(client, JobKind.COMPARE, job_id, formats, outdir, stem,
                      [args.original, args.modified], args.timeout)


def cmd_merge(args: argparse.Namespace, key: str) -> int:
    formats = _parse_formats(args.format, vsc.MERGE_FORMATS)
    if len(args.revisions) < vsc.MIN_MERGE_REVISIONS:
        raise ValueError(f"merge needs at least {vsc.MIN_MERGE_REVISIONS} revisions")
    for path in (args.original, *args.revisions):
        vsc.validate_input(path)

    outdir = args.outdir or args.original.parent
    stem = args.name or f"{args.original.stem}_vsmerge_{_stamp()}"

    client = vsc.Client(key)
    job_id = client.merge(args.original, args.revisions)
    print(f"merge_id: {job_id}")
    return _fetch_all(client, JobKind.MERGE, job_id, formats, outdir, stem,
                      [args.original, *args.revisions], args.timeout)


def cmd_edit_initiate(args: argparse.Namespace, key: str) -> int:
    vsc.validate_input(args.document)
    outdir = args.outdir or args.document.parent

    client = vsc.Client(key)
    file_id = client.edit_initiate(args.document)
    body = client.edit_markdown(file_id, timeout_s=args.timeout)

    outdir.mkdir(parents=True, exist_ok=True)
    target = _unique(outdir / f"{args.document.stem}_vsedit_{_stamp()}.md")
    target.write_text(f"<!-- versionstory file_id: {file_id} -->\n{body.get('markdown', '')}", encoding="utf-8")
    print(f"file_id: {file_id}")
    print(f"Wrote: {target}")
    return EXIT_OK


def cmd_edit(args: argparse.Namespace, key: str) -> int:
    formats = _parse_formats(args.format, vsc.EDIT_FORMATS)
    edits_text = args.edits.read_text(encoding="utf-8")
    vsc.validate_edits(json.loads(edits_text))

    if not args.file_id:
        vsc.validate_input(args.document)
    outdir = args.outdir or args.document.parent
    stem = args.name or f"{args.document.stem}_vsedit_{_stamp()}"

    client = vsc.Client(key)
    job_id = client.edit(
        edits_text,
        file_id=args.file_id,
        document=None if args.file_id else args.document,
        description=args.description,
    )
    print(f"edit_id: {job_id}")
    return _fetch_all(client, JobKind.EDIT, job_id, formats, outdir, stem, [args.document], args.timeout)


# -- parser ------------------------------------------------------------------

def _add_output_args(parser: argparse.ArgumentParser, default_formats: str) -> None:
    parser.add_argument("--format", default=default_formats, help=f"Comma-separated formats (default: {default_formats})")
    parser.add_argument("--outdir", type=Path, default=None, help="Output folder (default: next to the input)")
    parser.add_argument("--name", default=None, help="Output file stem (default: derived from inputs + date)")
    parser.add_argument("--timeout", type=int, default=vsc.DEFAULT_TIMEOUT_S, help="Seconds to wait for the job")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vs.py",
        description="Version Story hosted API: compare, merge, or edit .docx/.doc/.pdf documents.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("check", help="Verify the API key is set and accepted")
    p.set_defaults(func=cmd_check, needs_key=False)

    p = sub.add_parser("set-key", help="Save an API key to the repo-root .env")
    p.add_argument("key")
    p.add_argument("--no-verify", action="store_true", help="Skip the live auth probe")
    p.set_defaults(func=cmd_set_key, needs_key=False)

    p = sub.add_parser("compare", help="Redline MODIFIED against ORIGINAL")
    p.add_argument("original", type=Path)
    p.add_argument("modified", type=Path)
    p.add_argument("--author", default=DEFAULT_AUTHOR, help=f"Track-changes author (default: {DEFAULT_AUTHOR})")
    _add_output_args(p, "docx")
    p.set_defaults(func=cmd_compare, needs_key=True)

    p = sub.add_parser("merge", help="Merge two or more reviewers' revisions of ORIGINAL")
    p.add_argument("original", type=Path)
    p.add_argument("revisions", type=Path, nargs="+")
    _add_output_args(p, "docx")
    p.set_defaults(func=cmd_merge, needs_key=True)

    p = sub.add_parser("edit-initiate", help="Upload DOCUMENT and fetch its editable markdown")
    p.add_argument("document", type=Path)
    p.add_argument("--outdir", type=Path, default=None)
    p.add_argument("--timeout", type=int, default=vsc.DEFAULT_TIMEOUT_S)
    p.set_defaults(func=cmd_edit_initiate, needs_key=True)

    p = sub.add_parser("edit", help="Apply an edits JSON array to DOCUMENT")
    p.add_argument("document", type=Path, help="Document to edit (names the output when --file-id is given)")
    p.add_argument("--edits", type=Path, required=True, help="JSON file: array of edit operations")
    p.add_argument("--file-id", default=None, help="file_id from edit-initiate; skips re-upload")
    p.add_argument("--description", default=None, help="Optional edit description")
    _add_output_args(p, "docx")
    p.set_defaults(func=cmd_edit, needs_key=True)

    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    _load_env(_repo_root())
    key = os.environ.get(vsc.ENV_KEY, "").strip() or None

    if args.needs_key and not key:
        print(SETUP_MESSAGE, file=sys.stderr)
        return EXIT_NO_KEY

    try:
        return args.func(args, key)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_FAIL
    except ApiError as exc:
        print(f"API error {exc.http_status} {exc.code}: {exc.message}", file=sys.stderr)
        if exc.request_id:
            print(f"request_id: {exc.request_id}", file=sys.stderr)
        return EXIT_FAIL
    except JobFailed as exc:
        print(f"Job failed: {exc.code}", file=sys.stderr)
        if exc.upstream_code:
            print(f"upstream: {exc.upstream_code}", file=sys.stderr)
        return EXIT_FAIL
    except TimeoutError as exc:
        print(f"Timeout: {exc} (raise --timeout and re-run; the job may still finish server-side)", file=sys.stderr)
        return EXIT_FAIL


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
