from __future__ import annotations

import sys

import httpx
from meilisearch.errors import MeilisearchApiError

from caption_cli import cli
from caption_cli.core import CliError, _stringify_error


def main() -> None:
    try:
        exit_code = cli.run()
    except CliError as exc:
        print(exc.message, file=sys.stderr)
        raise SystemExit(exc.exit_code) from exc
    except (httpx.HTTPError, MeilisearchApiError) as exc:
        print(_stringify_error(exc), file=sys.stderr)
        raise SystemExit(1) from exc

    raise SystemExit(exit_code)
