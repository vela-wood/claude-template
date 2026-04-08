#!/usr/bin/env python3
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


@dataclass(frozen=True)
class EnvVarSpec:
    name: str
    description: str
    kind: str
    required: bool = False


ENV_VARS = (
    EnvVarSpec("ARTIFACT_API_TOKEN", "Artifact cleaning API token", "token"),
    EnvVarSpec("ARTIFACT_URL", "Artifact cleaning endpoint", "url"),
    EnvVarSpec("CAPTION_API_URL", "Caption API base URL", "url", required=True),
    EnvVarSpec("CAPTION_MEILI_URL", "Caption Meilisearch base URL", "url", required=True),
    EnvVarSpec("CLERK_API_KEY", "Clerk API key", "token", required=True),
    EnvVarSpec("MATTERS_DB", "NetDocs Postgres connection string", "dsn"),
    EnvVarSpec("ND_API_KEY", "NetDocs helper API key", "secret"),
    EnvVarSpec("NDHELPER_URL", "NetDocs helper base URL", "url"),
)


def redact_token(value: str) -> str:
    return f"{value[:4]}..."


def redact_dsn(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value

    if not parsed.scheme or not parsed.netloc:
        return value

    hostname = parsed.hostname or ""
    userinfo = ""
    if parsed.username:
        userinfo = parsed.username
    if parsed.password is not None:
        userinfo = f"{userinfo}:***" if userinfo else "***"

    hostinfo = hostname
    if parsed.port:
        hostinfo = f"{hostinfo}:{parsed.port}"

    netloc = f"{userinfo}@{hostinfo}" if userinfo else hostinfo
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def render_value(spec: EnvVarSpec, value: str) -> str:
    if spec.name.endswith("_TOKEN") or spec.name.endswith("_KEY") or spec.kind in {"token", "secret"}:
        return redact_token(value)
    if spec.kind == "dsn":
        return redact_dsn(value)
    return value


def main() -> int:
    from dotenv import load_dotenv

    env_file = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_file, override=True)

    missing_required: list[str] = []
    print("Environment check")
    print(f"Loaded dotenv: {env_file}")
    print()

    for spec in ENV_VARS:
        raw_value = os.getenv(spec.name)
        if raw_value:
            print(f"[OK] {spec.name}: {render_value(spec, raw_value)}")
            continue

        if spec.required:
            print(f"[MISSING] {spec.name}")
            missing_required.append(spec.name)
            continue

        if spec.kind != "url":
            print(f"[OPTIONAL] {spec.name}: not set")

    print()
    if missing_required:
        print("Error: required Caption environment is not configured.")
        print("Configure it here: https://dev.caption.fyi/claude_setup")
        print("Missing required vars:")
        for name in missing_required:
            print(f"  - {name}")
        return 1

    print("All required credentials present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
