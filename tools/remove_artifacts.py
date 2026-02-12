#!/usr/bin/env python3
import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dotenv import load_dotenv

load_dotenv()

ARTIFACT_URL = os.environ.get("ARTIFACT_URL")
ARTIFACT_API_TOKEN = os.environ.get("ARTIFACT_API_TOKEN")

def _derive_output_path(input_path: str) -> str:
    base, ext = os.path.splitext(input_path)
    return f"{base}_cleaned{ext or '.md'}"


def _post_clean_request(text: str, max_tokens: int) -> dict:
    if not ARTIFACT_URL:
        raise RuntimeError("ARTIFACT_URL is not set")
    if not ARTIFACT_API_TOKEN:
        raise RuntimeError("Missing token. Set ARTIFACT_API_KEY.")
    payload = {
        "transcript_text": text,
        "max_tokens_per_chunk": max_tokens,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(ARTIFACT_URL, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {ARTIFACT_API_TOKEN}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Request failed: {e.reason}") from e

    try:
        return json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Invalid JSON response: {raw[:200]}") from e


def _extract_cleaned_text(resp: dict) -> str:
    chunks = resp.get("chunks")
    if not isinstance(chunks, list):
        raise RuntimeError("Response missing 'chunks' list")
    cleaned_parts = []
    for idx, chunk in enumerate(chunks):
        if not isinstance(chunk, dict) or "cleaned_text" not in chunk:
            raise RuntimeError(f"Chunk {idx} missing 'cleaned_text'")
        cleaned_parts.append(chunk["cleaned_text"])
    return "\n\n".join(part.strip() for part in cleaned_parts if part is not None)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remove PDF artifacts via cleaning API."
    )
    parser.add_argument("input_md", help="Path to input markdown file")
    parser.add_argument(
        "--max-tokens-per-chunk",
        type=int,
        default=1000,
        help="Max tokens per chunk for backend processing",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Optional output path. Defaults to input name with _cleaned suffix.",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.input_md):
        print(f"Input not found: {args.input_md}", file=sys.stderr)
        return 2
    with open(args.input_md, "r", encoding="utf-8") as f:
        text = f.read()

    resp = _post_clean_request(
        text=text,
        max_tokens=args.max_tokens_per_chunk,
    )
    cleaned = _extract_cleaned_text(resp)

    output_path = args.output or _derive_output_path(args.input_md)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(cleaned)
        if not cleaned.endswith("\n"):
            f.write("\n")

    print(f"Wrote cleaned file: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
