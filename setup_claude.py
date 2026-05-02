#!/usr/bin/env python3
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

import httpx
from dotenv.parser import parse_stream

SETUP_PAGE_URL = "https://app.caption.fyi/claude_setup"
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
    appended_new_keys: tuple[str, ...]
    appended_conflicting_keys: tuple[str, ...]
    skipped_existing_keys: tuple[str, ...]


class SetupError(Exception):
    pass


def _clean_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    return cleaned


def drop_nulls(value: object) -> object:
    if isinstance(value, Mapping):
        cleaned_mapping = {
            key: drop_nulls(child_value)
            for key, child_value in value.items()
            if child_value is not None
        }
        if (
            any(field in cleaned_mapping for field in NAMED_CREDENTIAL_KEY_FIELDS)
            and not any(field in cleaned_mapping for field in NAMED_CREDENTIAL_VALUE_FIELDS)
        ):
            return None
        return cleaned_mapping
    if isinstance(value, list):
        cleaned_items: list[object] = []
        for item in value:
            if item is None:
                continue
            cleaned_item = drop_nulls(item)
            if cleaned_item is None:
                continue
            cleaned_items.append(cleaned_item)
        return cleaned_items
    return value


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


def collect_organization_metadata(
    env_values: dict[str, str],
    skipped_null_keys: set[str],
    organization: Mapping[str, object],
    *,
    source: str,
) -> None:
    organization_id = organization.get("organization_id")
    if organization_id is None:
        skipped_null_keys.add("ORGANIZATION_ID")
        return

    merge_env_value(
        env_values,
        "ORGANIZATION_ID",
        organization_id,
        source=f"{source}.organization_id",
    )


def choose_organization_payload(
    payload: Mapping[str, object],
    *,
    prompt: Callable[[str], str] = input,
    output: Callable[[str], None] = print,
) -> Mapping[str, object]:
    organizations = payload.get("organizations", [])
    if organizations is None:
        return payload
    if not isinstance(organizations, list):
        raise SetupError("'organizations' must be an array.")
    if len(organizations) <= 1:
        return payload

    validated_organizations: list[Mapping[str, object]] = []
    output("Multiple organizations found. Select which organization's credentials to load:")
    for index, organization in enumerate(organizations, start=1):
        if not isinstance(organization, Mapping):
            raise SetupError(f"'organizations[{index}]' must be an object.")
        validated_organizations.append(organization)
        organization_name = _clean_optional_text(organization.get("organization_name")) or f"Organization {index}"
        organization_id = _clean_optional_text(organization.get("organization_id")) or "<missing organization_id>"
        output(f"  {index}. {organization_name}: {organization_id}")

    while True:
        raw_selection = prompt(f"Select organization to load [1-{len(validated_organizations)}]: ").strip()
        if not raw_selection:
            output("Enter the number of the organization to load.")
            continue
        if not raw_selection.isdigit():
            output("Enter a valid number.")
            continue
        selected_index = int(raw_selection)
        if 1 <= selected_index <= len(validated_organizations):
            selected_payload = dict(payload)
            selected_payload["organizations"] = [validated_organizations[selected_index - 1]]
            return selected_payload
        output(f"Enter a number between 1 and {len(validated_organizations)}.")


def build_env_values(payload: Mapping[str, object]) -> BuildResult:
    env_values: dict[str, str] = {}
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
        collect_organization_metadata(
            env_values,
            skipped_null_keys,
            organization,
            source=f"organizations[{index}]",
        )
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

    if response.status_code >= 400:
        detail = response.text.strip() or response.reason_phrase
        raise SetupError(f"Setup request failed ({response.status_code}): {detail}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise SetupError("Setup request returned invalid JSON.") from exc

    if not isinstance(payload, dict):
        raise SetupError("Setup request returned non-object JSON.")

    cleaned_payload = drop_nulls(payload)
    if not isinstance(cleaned_payload, dict):
        raise SetupError("Setup request returned invalid object JSON.")
    return cleaned_payload


def prompt_for_token() -> str:
    print(f"Open {SETUP_PAGE_URL} in your browser (shift/cmd + click) and paste in the authentication token")
    token = input("Authentication token: ").strip()
    if not token:
        raise SetupError("No authentication token provided.")
    return token


def read_existing_env_values(env_file: Path) -> dict[str, list[str]]:
    if not env_file.exists():
        return {}

    values_by_key: dict[str, list[str]] = {}
    with env_file.open(encoding="utf-8") as source:
        for binding in parse_stream(source):
            if binding.key is None or binding.value is None:
                continue
            values_by_key.setdefault(binding.key, []).append(binding.value)
    return values_by_key


def render_env_line(key: str, value: str) -> str:
    quote = not value.isalnum()
    rendered_value = "'" + value.replace("'", "\\'") + "'" if quote else value
    return f"{key}={rendered_value}\n"


def write_env_file(env_file: Path, new_values: Mapping[str, str]) -> WriteResult:
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.touch(exist_ok=True)

    existing_values = read_existing_env_values(env_file)
    appended_new_keys: list[str] = []
    appended_conflicting_keys: list[str] = []
    skipped_existing_keys: list[str] = []
    lines_to_append: list[str] = []

    for key in sorted(new_values):
        new_value = new_values[key]
        current_values = existing_values.get(key, [])

        if not current_values:
            lines_to_append.append(render_env_line(key, new_value))
            appended_new_keys.append(key)
            continue

        if new_value in current_values:
            skipped_existing_keys.append(key)
            continue

        lines_to_append.append(render_env_line(key, new_value))
        appended_conflicting_keys.append(key)

    if lines_to_append:
        needs_newline = env_file.stat().st_size > 0 and not env_file.read_text(encoding="utf-8").endswith("\n")
        with env_file.open("a", encoding="utf-8") as destination:
            if needs_newline:
                destination.write("\n")
            destination.writelines(lines_to_append)

    return WriteResult(
        appended_new_keys=tuple(appended_new_keys),
        appended_conflicting_keys=tuple(appended_conflicting_keys),
        skipped_existing_keys=tuple(skipped_existing_keys),
    )


def main() -> int:
    env_file = Path(__file__).resolve().parent / ".env"

    try:
        auth_token = prompt_for_token()
        payload = fetch_setup_payload(auth_token)
        selected_payload = choose_organization_payload(payload)
        build_result = build_env_values(selected_payload)
        write_result = write_env_file(env_file, build_result.env_values)
    except SetupError as exc:
        print(f"Error: {exc}")
        return 1
    except httpx.HTTPError as exc:
        print(f"Error: {exc}")
        return 1

    print(f"Wrote environment to {env_file}")
    if write_result.appended_new_keys:
        print("Appended new keys:")
        for key in write_result.appended_new_keys:
            print(f"  - {key}")
    if write_result.appended_conflicting_keys:
        print("Appended additional values for existing keys:")
        for key in write_result.appended_conflicting_keys:
            print(f"  - {key}")
    if write_result.skipped_existing_keys:
        print("Skipped keys already present with the same value:")
        for key in write_result.skipped_existing_keys:
            print(f"  - {key}")
    if build_result.skipped_null_keys:
        print("Skipped null values for:")
        for key in build_result.skipped_null_keys:
            print(f"  - {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
