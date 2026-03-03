from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import httpx
import meilisearch

DEFAULT_CACHE_PATH = "search-token.json"
DEFAULT_LIMIT = 5
DEFAULT_SEARCH_INDEX = "transcript_captions_v1"
WORKSPACE_LIST_PAGE_SIZE = 100
PROJECT_OUTPUT_FIELDS = (
    "id",
    "transcript",
    "createdAt",
    "updatedAt",
    "name",
    "folder",
    "description",
)
FOLDER_OUTPUT_FIELDS = (
    "id",
    "createdAt",
    "updatedAt",
    "name",
    "parent",
    "description",
)


@dataclass(slots=True)
class CliError(Exception):
    message: str
    exit_code: int = 1


@dataclass(slots=True)
class SearchToken:
    token: str
    url: str | None = None
    expires_at: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "SearchToken":
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise CliError("/search/token response missing string 'token'")

        raw_url = payload.get("url")
        url = raw_url if isinstance(raw_url, str) and raw_url else None
        raw_expires = payload.get("expiresAt")
        expires_at = raw_expires if isinstance(raw_expires, str) and raw_expires else None
        return cls(token=token, url=url, expires_at=expires_at)

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"token": self.token}
        if self.url:
            payload["url"] = self.url
        if self.expires_at:
            payload["expiresAt"] = self.expires_at
        return payload


@dataclass(slots=True)
class RuntimeConfig:
    api_url: str | None
    api_token: str | None
    meili_url: str | None
    cache_path: Path
    output: str


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    handler: Callable[[RuntimeConfig, argparse.Namespace], Any]
    needs_meili: bool = False
    default_output: str = "json"
    usage: str = ""
    notes: tuple[str, ...] = ()
    example: str = ""



def fetch_search_token(api_url: str, api_token: str) -> SearchToken:
    url = f"{api_url.rstrip('/')}/search/token"
    headers = {"Authorization": f"Bearer {api_token}"}
    with httpx.Client(timeout=15.0) as client:
        response = client.get(url, headers=headers)

    if response.status_code >= 400:
        detail = response.text.strip() or response.reason_phrase
        raise CliError(f"Failed to fetch search token ({response.status_code}): {detail}")

    payload = response.json()
    if not isinstance(payload, dict):
        raise CliError("/search/token returned non-object JSON")
    return SearchToken.from_payload(payload)


def _authorized_request(
    api_url: str,
    api_token: str,
    method: str,
    path: str,
    params: Mapping[str, int] | None = None,
    json_body: Mapping[str, Any] | None = None,
    expected_statuses: set[int] | None = None,
) -> Mapping[str, Any]:
    url = f"{api_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {api_token}"}
    with httpx.Client(timeout=15.0) as client:
        response = client.request(method, url, headers=headers, params=params, json=json_body)

    if expected_statuses is None:
        is_error = response.status_code >= 400
    else:
        is_error = response.status_code not in expected_statuses
    if is_error:
        detail = response.text.strip() or response.reason_phrase
        raise CliError(f"Failed {method.upper()} {path} ({response.status_code}): {detail}")

    payload = response.json()
    if not isinstance(payload, dict):
        raise CliError(f"{path} returned non-object JSON")
    return payload


def _authorized_get(
    api_url: str,
    api_token: str,
    path: str,
    params: Mapping[str, int] | None = None,
) -> Mapping[str, Any]:
    return _authorized_request(api_url, api_token, "GET", path, params=params)


def _authorized_get_list_of_objects(
    api_url: str,
    api_token: str,
    path: str,
) -> list[Mapping[str, Any]]:
    url = f"{api_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {api_token}"}
    with httpx.Client(timeout=15.0) as client:
        response = client.get(url, headers=headers)

    if response.status_code != 200:
        detail = response.text.strip() or response.reason_phrase
        raise CliError(f"Failed GET {path} ({response.status_code}): {detail}")

    payload = response.json()
    if not isinstance(payload, list):
        raise CliError(f"{path} returned non-array JSON")
    if not all(isinstance(item, dict) for item in payload):
        raise CliError(f"{path} returned array containing non-object items")
    return payload


def _field_view(payload: Mapping[str, Any], fields: Sequence[str]) -> dict[str, Any]:
    return {key: payload.get(key) for key in fields}


def _project_view(project_payload: Mapping[str, Any]) -> dict[str, Any]:
    return _field_view(project_payload, PROJECT_OUTPUT_FIELDS)


def _folder_view(folder_payload: Mapping[str, Any]) -> dict[str, Any]:
    return _field_view(folder_payload, FOLDER_OUTPUT_FIELDS)


def fetch_current_workspace_id(api_url: str, api_token: str) -> str:
    payload = _authorized_get(api_url, api_token, "/users/me/workspace")
    workspace_id = payload.get("id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        raise CliError("/users/me/workspace response missing string 'id'")
    return workspace_id


def fetch_workspace_items_page(
    api_url: str,
    api_token: str,
    workspace_id: str,
    endpoint: str,
    *,
    page: int,
    limit: int = WORKSPACE_LIST_PAGE_SIZE,
) -> Mapping[str, Any]:
    return _authorized_get(
        api_url,
        api_token,
        f"/workspaces/{workspace_id}/{endpoint}",
        params={"page": page, "limit": limit},
    )


def load_cached_search_token(cache_path: Path) -> SearchToken | None:
    if not cache_path.exists():
        return None

    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise CliError(f"Failed reading cache file {cache_path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise CliError(f"Cache file {cache_path} must contain a JSON object")

    return SearchToken.from_payload(payload)


def save_search_token(cache_path: Path, search_token: SearchToken) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(search_token.to_payload(), indent=2) + "\n", encoding="utf-8")


def _require_api_url(config: RuntimeConfig) -> str:
    if config.api_url and config.api_url.strip():
        return config.api_url.strip()
    raise CliError("Missing Caption API URL. Set CAPTION_API_URL")


def _require_meili_url(config: RuntimeConfig) -> str:
    if config.meili_url and config.meili_url.strip():
        return config.meili_url.strip()
    raise CliError("Missing Meilisearch URL. Set CAPTION_MEILI_URL")


def resolve_meili_url(config: RuntimeConfig) -> str:
    return _require_meili_url(config)


def build_meili_client(meili_url: str, meili_token: str) -> meilisearch.Client:
    return meilisearch.Client(meili_url, api_key=meili_token)


def _stringify_error(err: Exception) -> str:
    message = getattr(err, "message", None)
    if isinstance(message, str) and message:
        return message
    return str(err)


def _is_meili_auth_error(err: Exception) -> bool:
    status_code = getattr(err, "status_code", None)
    if status_code in {401, 403}:
        return True

    code = getattr(err, "code", None)
    if isinstance(code, str) and code in {"invalid_api_key", "missing_authorization_header"}:
        return True

    message = _stringify_error(err).lower()
    return any(token in message for token in ("invalid_api_key", "api key", "unauthorized", "forbidden"))


def _require_api_token(config: RuntimeConfig) -> str:
    if config.api_token and config.api_token.strip():
        return config.api_token.strip()
    raise CliError("Missing API bearer token. Set CAPTION_TOKEN")


def _require_cached_or_fresh_token(config: RuntimeConfig) -> SearchToken:
    cached = load_cached_search_token(config.cache_path)
    if cached:
        return cached

    api_token = _require_api_token(config)
    fresh = fetch_search_token(_require_api_url(config), api_token)
    save_search_token(config.cache_path, fresh)
    return fresh


def _run_with_single_auth_retry(
    config: RuntimeConfig,
    operation: Callable[[meilisearch.Client], Any],
    initial_token: SearchToken,
) -> Any:
    initial_url = resolve_meili_url(config)
    client = build_meili_client(initial_url, initial_token.token)

    try:
        return operation(client)
    except Exception as exc:
        if not _is_meili_auth_error(exc):
            raise CliError(_stringify_error(exc)) from exc

        api_token = _require_api_token(config)
        refreshed_token = fetch_search_token(_require_api_url(config), api_token)
        save_search_token(config.cache_path, refreshed_token)

        retry_url = resolve_meili_url(config)
        retry_client = build_meili_client(retry_url, refreshed_token.token)

        try:
            return operation(retry_client)
        except Exception as retry_exc:
            raise CliError(_stringify_error(retry_exc)) from retry_exc


def _render_table(value: Any) -> str:
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            lines: list[str] = []
            workspace_id = value.get("workspaceId")
            if workspace_id:
                lines.append(f"workspaceId: {workspace_id}")
            lines.append(f"count: {len(items)}")
            if not items:
                return "\n".join(lines)
            columns = list(items[0].keys())
            lines.append("\t".join(columns))
            for item in items:
                lines.append("\t".join("" if item.get(column) is None else str(item.get(column)) for column in columns))
            return "\n".join(lines)

        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                lines.append(f"{key}: {json.dumps(item, ensure_ascii=True)}")
            else:
                lines.append(f"{key}: {item}")
        return "\n".join(lines)

    if isinstance(value, list):
        return "\n".join(json.dumps(item, ensure_ascii=True) for item in value)

    return str(value)


def _truncate_for_cell(value: Any, *, limit: int = 72) -> str:
    text = str(value).replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return f"{text[: limit - 3]}..."


def _cell(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("|", "\\|")


def _to_yyyymmdd(value: Any) -> str:
    if not isinstance(value, str) or len(value) < 10:
        return ""
    candidate = value[:10]
    if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
        return candidate.replace("-", "")
    return ""


def _search_summary_header(value: Mapping[str, Any], hits: Sequence[Mapping[str, Any]]) -> list[str]:
    return [f"estimatedTotalHits: {value.get('estimatedTotalHits')} | returned: {len(hits)}"]


def _render_search_summary_table(value: Any, index_uid: str | None) -> str:
    if not isinstance(value, dict):
        return _render_table(value)
    raw_hits = value.get("hits")
    if not isinstance(raw_hits, list):
        return _render_table(value)
    hits = [hit for hit in raw_hits if isinstance(hit, dict)]

    lines = _search_summary_header(value, hits)
    if not hits:
        lines.append("No hits.")
        return "\n".join(lines)

    if index_uid == "transcript_captions_v1":
        lines.append("| # | scope.projectId (uuid) | speaker.name | updatedAt (YYYYMMDD) | content |")
        lines.append("|---:|---|---|---|---|")
        for i, hit in enumerate(hits, start=1):
            scope = hit.get("scope") if isinstance(hit.get("scope"), dict) else {}
            speaker = hit.get("speaker") if isinstance(hit.get("speaker"), dict) else {}
            row = [
                str(i),
                _cell(scope.get("projectId")),
                _cell(speaker.get("name")),
                _cell(_to_yyyymmdd(hit.get("updatedAt"))),
                _cell(_truncate_for_cell(hit.get("content", ""))),
            ]
            lines.append(f"| {' | '.join(row)} |")
        return "\n".join(lines)

    if index_uid == "transcript_sessions_v1":
        lines.append("| # | scope.projectId (uuid) | updatedAt (YYYYMMDD) | speakers | content |")
        lines.append("|---:|---|---|---|---|")
        for i, hit in enumerate(hits, start=1):
            scope = hit.get("scope") if isinstance(hit.get("scope"), dict) else {}
            speakers_value = hit.get("speakers")
            speakers = ", ".join(str(item) for item in speakers_value) if isinstance(speakers_value, list) else ""
            row = [
                str(i),
                _cell(scope.get("projectId")),
                _cell(_to_yyyymmdd(hit.get("updatedAt"))),
                _cell(_truncate_for_cell(speakers, limit=40)),
                _cell(_truncate_for_cell(hit.get("content", ""))),
            ]
            lines.append(f"| {' | '.join(row)} |")
        return "\n".join(lines)

    if index_uid == "projects_v1":
        lines.append("| # | id (project uuid) | updatedAt (YYYYMMDD) | name | description |")
        lines.append("|---:|---|---|---|---|")
        for i, hit in enumerate(hits, start=1):
            description = "" if hit.get("description") is None else _truncate_for_cell(hit.get("description", ""), limit=60)
            row = [
                str(i),
                _cell(hit.get("id")),
                _cell(_to_yyyymmdd(hit.get("updatedAt"))),
                _cell(_truncate_for_cell(hit.get("name", ""), limit=40)),
                _cell(description),
            ]
            lines.append(f"| {' | '.join(row)} |")
        return "\n".join(lines)

    # Unknown search index: retain existing generic table rendering behavior.
    return _render_table(value)


def _parse_iso_datetime(raw_value: str, *, label: str) -> datetime:
    normalized = raw_value
    if raw_value.endswith("Z"):
        normalized = f"{raw_value[:-1]}+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise CliError(f"{label} must be an ISO datetime string, got: {raw_value}") from exc


def _speaker_label_for_transcript_item(item: Mapping[str, Any], *, item_index: int) -> str:
    channel = item.get("channel")
    if not isinstance(channel, int):
        raise CliError(f"Transcript item {item_index} missing integer 'channel'")

    diarization_index = item.get("index")
    if not isinstance(diarization_index, int):
        raise CliError(f"Transcript item {item_index} missing integer 'index'")

    if channel == 0:
        return "me"
    if channel == 1:
        return f"meeting-{diarization_index}"

    raise CliError(f"Transcript item {item_index} has unsupported channel: {channel}")


def _transcript_items_to_md(items: Sequence[Mapping[str, Any]]) -> str:
    grouped_entries: list[dict[str, Any]] = []
    paragraph: dict[str, Any] | None = None

    for item_index, item in enumerate(items):
        created_at = item.get("createdAt")
        if not isinstance(created_at, str):
            raise CliError(f"Transcript item {item_index} missing string 'createdAt'")
        created_at_dt = _parse_iso_datetime(created_at, label=f"Transcript item {item_index} field 'createdAt'")

        content = item.get("content")
        if not isinstance(content, str):
            raise CliError(f"Transcript item {item_index} missing string 'content'")

        speaker = _speaker_label_for_transcript_item(item, item_index=item_index)
        if paragraph is None or paragraph["speaker"] != speaker:
            if paragraph is not None:
                grouped_entries.append(paragraph)
            paragraph = {
                "speaker": speaker,
                "earliest_timestamp": created_at_dt,
                "content_parts": [content],
            }
            continue

        paragraph["content_parts"].append(content)
        if created_at_dt < paragraph["earliest_timestamp"]:
            paragraph["earliest_timestamp"] = created_at_dt

    if paragraph is not None:
        grouped_entries.append(paragraph)

    markdown_lines: list[str] = []
    for entry in grouped_entries:
        formatted_time = entry["earliest_timestamp"].strftime("%H:%M.%S")
        merged_content = " ".join(entry["content_parts"])
        markdown_lines.append(f"[{formatted_time}] {entry['speaker']}: {merged_content}")
    return "\n".join(markdown_lines)


def _is_transcript_payload(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    transcript_id = value.get("transcriptId")
    items = value.get("items")
    return isinstance(transcript_id, str) and isinstance(items, list)


def emit_output(
    value: Any,
    output_format: str,
    *,
    command_name: str | None = None,
    search_index: str | None = None,
) -> None:
    if output_format == "json":
        print(json.dumps(value, indent=2))
        return
    if output_format == "md" and _is_transcript_payload(value):
        items = value["items"]
        if not all(isinstance(item, dict) for item in items):
            raise CliError("dl_transcript payload field 'items' must be an array of objects")
        print(_transcript_items_to_md(items))
        return
    if output_format == "table" and command_name == "search":
        print(_render_search_summary_table(value, search_index))
        return
    print(_render_table(value))
