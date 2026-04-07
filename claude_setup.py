#!/usr/bin/env python3
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
from dotenv import dotenv_values, set_key

SETUP_PAGE_URL = "https://dev.caption.fyi/claude_setup"
SETUP_API_URL = "https://chat.caption.fyi/claude_setup"
ROOT_KEYS_TO_SKIP = {"primary_email_address", "organizations"}
NAMED_CREDENTIAL_KEY_FIELDS = ("name", "key", "env", "env_var", "variable")
NAMED_CREDENTIAL_VALUE_FIELDS = ("value", "token", "secret", "credential", "url", "api_key")


@dataclass(frozen=True)
class BuildResult:
    env_values: dict[str, str]
    skipped_null_keys: tuple[str, ...]


@dataclass(frozen=True)
class WriteResult:
    added_keys: tuple[str, ...]
    updated_keys: tuple[str, ...]
    preserved_conflicts: tuple[str, ...]


class SetupError(Exception):
    pass


def normalize_env_key(raw_key: str) -> str:
    normalized = re.sub(r"[^0-9A-Za-z]+", "_", raw_key).strip("_").upper()
    if not normalized:
        raise SetupError(f"Cannot convert {raw_key!r} into an environment variable name.")
    if normalized[0].isdigit():
        normalized = f"KEY_{normalized}"
    return normalized


def stringify_scalar(value: object, *, source: str) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (str, int, float)):
        return str(value)
    raise SetupError(f"Unsupported non-scalar value at {source}.")


def merge_env_value(env_values: dict[str, str], key: str, value: object, *, source: str) -> None:
    string_value = stringify_scalar(value, source=source)
    existing_value = env_values.get(key)
    if existing_value is not None and existing_value != string_value:
        raise SetupError(
            f"Conflicting values for {key}: {existing_value!r} from earlier data and {string_value!r} from {source}."
        )
    env_values[key] = string_value


def collect_prefixed_values(
    env_values: dict[str, str],
    skipped_null_keys: set[str],
    key_prefix: str,
    value: object,
    *,
    source: str,
) -> None:
    if value is None:
        skipped_null_keys.add(key_prefix)
        return

    if isinstance(value, Mapping):
        for child_key, child_value in value.items():
            child_prefix = f"{key_prefix}_{normalize_env_key(str(child_key))}"
            collect_prefixed_values(
                env_values,
                skipped_null_keys,
                child_prefix,
                child_value,
                source=f"{source}.{child_key}",
            )
        return

    if isinstance(value, list):
        for index, item in enumerate(value, start=1):
            child_prefix = f"{key_prefix}_{index}"
            collect_prefixed_values(
                env_values,
                skipped_null_keys,
                child_prefix,
                item,
                source=f"{source}[{index}]",
            )
        return

    merge_env_value(env_values, key_prefix, value, source=source)


def extract_named_credential(item: Mapping[str, object]) -> tuple[str, object] | None:
    raw_name: object | None = None
    for field in NAMED_CREDENTIAL_KEY_FIELDS:
        candidate = item.get(field)
        if isinstance(candidate, str) and candidate.strip():
            raw_name = candidate
            break

    if raw_name is None:
        return None

    for field in NAMED_CREDENTIAL_VALUE_FIELDS:
        if field in item:
            return normalize_env_key(raw_name), item[field]

    raise SetupError(f"Credential entry for {raw_name!r} is missing a value field.")


def collect_organization_credentials(
    env_values: dict[str, str],
    skipped_null_keys: set[str],
    credentials: object,
    *,
    source: str,
) -> None:
    if credentials is None:
        return

    if isinstance(credentials, Mapping):
        for key, value in credentials.items():
            collect_prefixed_values(
                env_values,
                skipped_null_keys,
                normalize_env_key(str(key)),
                value,
                source=f"{source}.{key}",
            )
        return

    if isinstance(credentials, list):
        for index, item in enumerate(credentials, start=1):
            item_source = f"{source}[{index}]"
            if item is None:
                continue
            if not isinstance(item, Mapping):
                raise SetupError(f"Unsupported credential entry at {item_source}.")

            named_credential = extract_named_credential(item)
            if named_credential is not None:
                key, value = named_credential
                if value is None:
                    skipped_null_keys.add(key)
                    continue
                if isinstance(value, Mapping) or isinstance(value, list):
                    collect_prefixed_values(env_values, skipped_null_keys, key, value, source=item_source)
                    continue
                merge_env_value(env_values, key, value, source=item_source)
                continue

            for key, value in item.items():
                collect_prefixed_values(
                    env_values,
                    skipped_null_keys,
                    normalize_env_key(str(key)),
                    value,
                    source=f"{item_source}.{key}",
                )
        return

    raise SetupError(f"Unsupported credentials payload at {source}.")


def build_env_values(payload: Mapping[str, object], auth_token: str) -> BuildResult:
    env_values: dict[str, str] = {"CAPTION_TOKEN": auth_token}
    skipped_null_keys: set[str] = set()

    organizations = payload.get("organizations", [])
    if organizations is None:
        organizations = []
    if not isinstance(organizations, list):
        raise SetupError("'organizations' must be an array.")

    for key, value in payload.items():
        if key in ROOT_KEYS_TO_SKIP:
            continue
        collect_prefixed_values(
            env_values,
            skipped_null_keys,
            normalize_env_key(key),
            value,
            source=f"payload.{key}",
        )

    for index, organization in enumerate(organizations, start=1):
        if not isinstance(organization, Mapping):
            raise SetupError(f"'organizations[{index}]' must be an object.")
        collect_organization_credentials(
            env_values,
            skipped_null_keys,
            organization.get("credentials"),
            source=f"organizations[{index}].credentials",
        )

    return BuildResult(
        env_values=env_values,
        skipped_null_keys=tuple(sorted(skipped_null_keys)),
    )


def fetch_setup_payload(auth_token: str) -> Mapping[str, object]:
    headers = {"Authorization": f"Bearer {auth_token}"}
    with httpx.Client(timeout=15.0) as client:
        response = client.get(SETUP_API_URL, headers=headers)

    print(f"HTTP {response.status_code}")
    print(response.text)

    if response.status_code >= 400:
        detail = response.text.strip() or response.reason_phrase
        raise SetupError(f"Setup request failed ({response.status_code}): {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SetupError("Setup request returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise SetupError("Setup request returned non-object JSON.")
    return payload


def detect_conflicts(existing_values: Mapping[str, str | None], new_values: Mapping[str, str]) -> tuple[str, ...]:
    conflicts = [
        key
        for key, new_value in new_values.items()
        if key in existing_values and existing_values[key] not in {None, new_value}
    ]
    return tuple(sorted(conflicts))


def prompt_for_token() -> str:
    print(f"Open {SETUP_PAGE_URL} in your browser (shift/cmd + click) and paste in the authentication token")
    token = input("Authentication token: ").strip()
    if not token:
        raise SetupError("No authentication token provided.")
    return token


def prompt_for_conflict_mode(conflicts: tuple[str, ...]) -> str:
    print("Conflicting keys already exist in .env:")
    for key in conflicts:
        print(f"  - {key}")

    while True:
        choice = input("Type 'overwrite' to replace them or 'append' to keep existing values: ").strip().lower()
        if choice in {"overwrite", "o"}:
            return "overwrite"
        if choice in {"append", "a"}:
            return "append"
        print("Invalid choice. Type 'overwrite' or 'append'.")


def write_env_file(env_file: Path, new_values: Mapping[str, str], *, mode: str) -> WriteResult:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch(exist_ok=True)

    existing_values = dotenv_values(env_file)
    added_keys: list[str] = []
    updated_keys: list[str] = []
    preserved_conflicts: list[str] = []

    for key in sorted(new_values):
        new_value = new_values[key]
        current_value = existing_values.get(key)

        if current_value is None:
            set_key(env_file, key, new_value, quote_mode="auto")
            added_keys.append(key)
            continue

        if current_value == new_value:
            continue

        if mode == "overwrite":
            set_key(env_file, key, new_value, quote_mode="auto")
            updated_keys.append(key)
            continue

        preserved_conflicts.append(key)

    return WriteResult(
        added_keys=tuple(added_keys),
        updated_keys=tuple(updated_keys),
        preserved_conflicts=tuple(preserved_conflicts),
    )


def main() -> int:
    env_file = Path(__file__).resolve().parent / ".env"

    try:
        auth_token = prompt_for_token()
        payload = fetch_setup_payload(auth_token)
        build_result = build_env_values(payload, auth_token)

        existing_values = dotenv_values(env_file) if env_file.exists() else {}
        conflicts = detect_conflicts(existing_values, build_result.env_values)
        mode = prompt_for_conflict_mode(conflicts) if conflicts else "overwrite"

        write_result = write_env_file(env_file, build_result.env_values, mode=mode)
    except SetupError as exc:
        print(f"Error: {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Wrote environment to {env_file}")
    if write_result.added_keys:
        print("Added keys:")
        for key in write_result.added_keys:
            print(f"  - {key}")
    if write_result.updated_keys:
        print("Updated keys:")
        for key in write_result.updated_keys:
            print(f"  - {key}")
    if write_result.preserved_conflicts:
        print("Kept existing values for:")
        for key in write_result.preserved_conflicts:
            print(f"  - {key}")
    if build_result.skipped_null_keys:
        print("Skipped null values for:")
        for key in build_result.skipped_null_keys:
            print(f"  - {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
