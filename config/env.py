"""Caption credentials task: fetch the setup payload and write .env.

Pure payload→env-values logic plus the httpx fetch; no TUI here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import httpx
from dotenv.parser import parse_stream

from .common import SetupError

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


def _clean_optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


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
        return [
            cleaned_item
            for item in value
            if (cleaned_item := drop_nulls(item)) is not None
        ]
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
                if isinstance(value, (Mapping, list)):
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


def organization_choices(payload: Mapping[str, object]) -> list[tuple[str, str]]:
    """Validated [(organization_name, organization_id), ...] from a payload."""
    organizations = payload.get("organizations", [])
    if organizations is None:
        return []
    if not isinstance(organizations, list):
        raise SetupError(
            "The setup code didn't include valid organization details. "
            "Get a fresh code from the setup page and try again."
        )
    choices: list[tuple[str, str]] = []
    for index, organization in enumerate(organizations, start=1):
        if not isinstance(organization, Mapping):
            raise SetupError(
                "The setup code didn't include valid organization details. "
                "Get a fresh code from the setup page and try again."
            )
        organization_name = _clean_optional_text(organization.get("organization_name")) or f"Organization {index}"
        # Empty id → the TUI shows the name alone (never a placeholder).
        organization_id = _clean_optional_text(organization.get("organization_id")) or ""
        choices.append((organization_name, organization_id))
    return choices


def select_organization(payload: Mapping[str, object], index: int) -> Mapping[str, object]:
    """Return payload narrowed to organizations[index] (0-based)."""
    organizations = payload.get("organizations", [])
    if not isinstance(organizations, list) or not 0 <= index < len(organizations):
        raise SetupError(f"No organization at index {index}.")
    selected_payload = dict(payload)
    selected_payload["organizations"] = [organizations[index]]
    return selected_payload


def build_env_values(payload: Mapping[str, object]) -> BuildResult:
    env_values: dict[str, str] = {}
    skipped_null_keys: set[str] = set()

    organizations = payload.get("organizations", [])
    if organizations is None:
        organizations = []
    if not isinstance(organizations, list):
        raise SetupError(
            "The setup code didn't include valid organization details. "
            "Get a fresh code from the setup page and try again."
        )

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
            raise SetupError(
                "The setup code didn't include valid organization details. "
                "Get a fresh code from the setup page and try again."
            )
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


def _payload_from_response(response: httpx.Response) -> Mapping[str, object]:
    # These messages are shown to non-technical users verbatim in the setup
    # screen; technical detail stays parenthesized at the end.
    _UNEXPECTED = (
        "The setup service sent back something unexpected. Try again; "
        "if it keeps failing, contact support."
    )
    if response.status_code >= 400:
        detail = response.text.strip() or response.reason_phrase
        raise SetupError(
            f"The setup service reported a problem (code "
            f"{response.status_code}). Check that the setup code was pasted "
            f"correctly, or try again in a minute; if it keeps failing, "
            f"contact support. ({detail})"
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise SetupError(_UNEXPECTED) from exc

    if not isinstance(payload, dict):
        raise SetupError(_UNEXPECTED)

    cleaned_payload = drop_nulls(payload)
    if not isinstance(cleaned_payload, dict):
        raise SetupError(_UNEXPECTED)
    return cleaned_payload


def fetch_setup_payload(auth_token: str) -> Mapping[str, object]:
    headers = {"Authorization": f"Bearer {auth_token}"}
    with httpx.Client(timeout=15.0) as client:
        response = client.get(SETUP_API_URL, headers=headers)
    return _payload_from_response(response)


async def fetch_setup_payload_async(auth_token: str) -> Mapping[str, object]:
    """Async twin of fetch_setup_payload for @work async workers (textual
    cancels them for real; thread workers would wait out the timeout)."""
    headers = {"Authorization": f"Bearer {auth_token}"}
    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(SETUP_API_URL, headers=headers)
    return _payload_from_response(response)


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


def summarize_env_write(build_result: BuildResult, write_result: WriteResult) -> str:
    """One-line toast for the credentials task, in plain language."""
    parts = [f"{len(write_result.appended_new_keys)} added"]
    if write_result.appended_conflicting_keys:
        parts.append(f"{len(write_result.appended_conflicting_keys)} updated")
    if write_result.skipped_existing_keys:
        parts.append(f"{len(write_result.skipped_existing_keys)} already saved")
    if build_result.skipped_null_keys:
        parts.append(f"{len(build_result.skipped_null_keys)} skipped (empty)")
    return "Saved your credentials: " + ", ".join(parts)
