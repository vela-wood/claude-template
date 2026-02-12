#!/usr/bin/env python3
"""Count tokens in a file using tiktoken."""

import argparse
import sys
from pathlib import Path

import tiktoken


def count_tokens(text: str, model: str = "cl100k_base") -> int:
    """Count tokens in text using the specified encoding."""
    encoding = tiktoken.get_encoding(model)
    return len(encoding.encode(text))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Count tokens in a file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("file", type=Path, help="File to count tokens in")
    parser.add_argument(
        "-e",
        "--encoding",
        default="cl100k_base",
        choices=["cl100k_base", "p50k_base", "r50k_base", "o200k_base"],
        help="Tokenizer encoding (default: cl100k_base, used by GPT-4/Claude)",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Show additional stats"
    )

    args = parser.parse_args()

    if not args.file.exists():
        print(f"Error: File not found: {args.file}", file=sys.stderr)
        return 1

    try:
        text = args.file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"Error: Cannot read file as text: {args.file}", file=sys.stderr)
        return 1

    token_count = count_tokens(text, args.encoding)

    if args.verbose:
        char_count = len(text)
        word_count = len(text.split())
        line_count = text.count("\n") + (1 if text and not text.endswith("\n") else 0)
        print(f"File: {args.file}")
        print(f"Encoding: {args.encoding}")
        print(f"Tokens: {token_count:,}")
        print(f"Characters: {char_count:,}")
        print(f"Words: {word_count:,}")
        print(f"Lines: {line_count:,}")
        print(f"Chars/token: {char_count / token_count:.2f}" if token_count else "N/A")
    else:
        print(token_count)

    return 0


if __name__ == "__main__":
    sys.exit(main())
