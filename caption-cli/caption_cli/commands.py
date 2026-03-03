from __future__ import annotations

from typing import Any, Callable, Mapping

import meilisearch

from caption_cli.core import (
    CliError,
    RuntimeConfig,
    WORKSPACE_LIST_PAGE_SIZE,
    _authorized_get_list_of_objects,
    _authorized_request,
    _folder_view,
    _project_view,
    _require_api_token,
    _require_api_url,
    _require_cached_or_fresh_token,
    _run_with_single_auth_retry,
    build_meili_client,
    fetch_current_workspace_id,
    fetch_search_token,
    fetch_workspace_items_page,
    resolve_meili_url,
    save_search_token,
)

def command_token(config: RuntimeConfig, *, show_token: bool = False) -> dict[str, Any]:
    api_token = _require_api_token(config)
    token_payload = fetch_search_token(_require_api_url(config), api_token)
    save_search_token(config.cache_path, token_payload)

    resolved_url = resolve_meili_url(config)
    return {
        "token": token_payload.token if show_token else "[REDACTED]",
        "url": resolved_url,
        "expiresAt": token_payload.expires_at,
        "cached": str(config.cache_path),
    }


def command_search(config: RuntimeConfig, query: str, index: str, limit: int) -> dict[str, Any]:
    resolved_index = index.strip()
    if not resolved_index:
        raise CliError("--index cannot be empty")

    token_payload = _require_cached_or_fresh_token(config)

    def _operation(client: meilisearch.Client) -> dict[str, Any]:
        return client.index(resolved_index).search(query, {"limit": limit})

    return _run_with_single_auth_retry(config, _operation, token_payload)


def _command_list_workspace_items(
    config: RuntimeConfig,
    *,
    endpoint: str,
    item_view: Callable[[Mapping[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    api_url = _require_api_url(config)
    api_token = _require_api_token(config)
    workspace_id = fetch_current_workspace_id(api_url, api_token)

    items_out: list[dict[str, Any]] = []
    page = 0
    total_pages: int | None = None
    total_count: int | None = None

    while True:
        payload = fetch_workspace_items_page(
            api_url,
            api_token,
            workspace_id,
            endpoint,
            page=page,
            limit=WORKSPACE_LIST_PAGE_SIZE,
        )
        raw_items = payload.get("items")
        path = f"/workspaces/{{workspaceId}}/{endpoint}"
        if not isinstance(raw_items, list):
            raise CliError(f"{path} response missing array 'items'")
        for item in raw_items:
            if not isinstance(item, dict):
                raise CliError(f"{path} response contains non-object item")
            items_out.append(item_view(item))

        raw_total_pages = payload.get("totalPages")
        if not isinstance(raw_total_pages, int):
            raise CliError(f"{path} response missing integer 'totalPages'")
        total_pages = raw_total_pages

        raw_total_count = payload.get("totalCount")
        if isinstance(raw_total_count, int):
            total_count = raw_total_count

        page += 1
        if page >= total_pages:
            break

    return {
        "items": items_out,
        "count": len(items_out),
        "totalPages": total_pages if total_pages is not None else 0,
    }


def command_list_projects(config: RuntimeConfig) -> dict[str, Any]:
    return _command_list_workspace_items(config, endpoint="projects", item_view=_project_view)


def command_list_folders(config: RuntimeConfig) -> dict[str, Any]:
    return _command_list_workspace_items(config, endpoint="folders", item_view=_folder_view)


def _resolve_workspace_id(api_url: str, api_token: str, workspace_id: str | None) -> str:
    if workspace_id is None:
        return fetch_current_workspace_id(api_url, api_token)
    resolved_workspace_id = workspace_id.strip()
    if not resolved_workspace_id:
        raise CliError("--workspace-id cannot be empty")
    return resolved_workspace_id


def _clean_required_id(identifier: str, label: str) -> str:
    cleaned_identifier = identifier.strip()
    if not cleaned_identifier:
        raise CliError(f"{label} cannot be empty")
    return cleaned_identifier


def _build_create_body(
    *,
    command_name: str,
    name: str,
    description: str | None,
    nullable_link_value: str | None = None,
    nullable_link_field: str | None = None,
    nullable_link_arg: str | None = None,
) -> dict[str, Any]:
    cleaned_name = name.strip()
    if not cleaned_name:
        raise CliError(f"{command_name} requires a non-empty name")

    body: dict[str, Any] = {"name": cleaned_name}
    if description is not None:
        body["description"] = description

    if nullable_link_field is not None and nullable_link_value is not None:
        cleaned_link = nullable_link_value.strip()
        if not cleaned_link:
            raise CliError(f"{nullable_link_arg} cannot be empty")
        body[nullable_link_field] = cleaned_link
    return body


def _build_edit_body(
    *,
    command_name: str,
    name: str | None,
    description: str | None,
    clear_description: bool,
    nullable_link_value: str | None,
    clear_nullable_link: bool,
    nullable_link_field: str,
    nullable_link_arg: str,
    clear_nullable_link_arg: str,
) -> dict[str, Any]:
    if clear_description and description is not None:
        raise CliError("Use either --description or --clear-description, not both")
    if clear_nullable_link and nullable_link_value is not None:
        raise CliError(f"Use either {nullable_link_arg} or {clear_nullable_link_arg}, not both")

    body: dict[str, Any] = {}
    if name is not None:
        cleaned_name = name.strip()
        if not cleaned_name:
            raise CliError("--name cannot be empty")
        body["name"] = cleaned_name
    if clear_description:
        body["description"] = None
    elif description is not None:
        body["description"] = description
    if clear_nullable_link:
        body[nullable_link_field] = None
    elif nullable_link_value is not None:
        cleaned_link = nullable_link_value.strip()
        if not cleaned_link:
            raise CliError(f"{nullable_link_arg} cannot be empty")
        body[nullable_link_field] = cleaned_link
    if not body:
        raise CliError(
            f"{command_name} requires at least one field: "
            f"--name, --description/--clear-description, {nullable_link_arg}/{clear_nullable_link_arg}"
        )
    return body


def command_create_project(
    config: RuntimeConfig,
    *,
    name: str,
    description: str | None,
    workspace_id: str | None,
) -> dict[str, Any]:
    api_url = _require_api_url(config)
    api_token = _require_api_token(config)
    resolved_workspace_id = _resolve_workspace_id(api_url, api_token, workspace_id)
    body = _build_create_body(command_name="create_project", name=name, description=description)

    payload = _authorized_request(
        api_url,
        api_token,
        "POST",
        f"/workspaces/{resolved_workspace_id}/projects",
        json_body=body,
        expected_statuses={201},
    )
    return _project_view(payload)


def command_create_folder(
    config: RuntimeConfig,
    *,
    name: str,
    description: str | None,
    parent: str | None,
    workspace_id: str | None,
) -> dict[str, Any]:
    api_url = _require_api_url(config)
    api_token = _require_api_token(config)
    resolved_workspace_id = _resolve_workspace_id(api_url, api_token, workspace_id)
    body = _build_create_body(
        command_name="create_folder",
        name=name,
        description=description,
        nullable_link_value=parent,
        nullable_link_field="parent",
        nullable_link_arg="--parent",
    )

    payload = _authorized_request(
        api_url,
        api_token,
        "POST",
        f"/workspaces/{resolved_workspace_id}/folders",
        json_body=body,
        expected_statuses={200},
    )
    return _folder_view(payload)


def command_edit_project(
    config: RuntimeConfig,
    *,
    project_id: str,
    name: str | None,
    description: str | None,
    clear_description: bool,
    folder: str | None,
    clear_folder: bool,
) -> dict[str, Any]:
    api_url = _require_api_url(config)
    api_token = _require_api_token(config)
    cleaned_project_id = _clean_required_id(project_id, "project_id")
    body = _build_edit_body(
        command_name="edit_project",
        name=name,
        description=description,
        clear_description=clear_description,
        nullable_link_value=folder,
        clear_nullable_link=clear_folder,
        nullable_link_field="folder",
        nullable_link_arg="--folder",
        clear_nullable_link_arg="--clear-folder",
    )

    payload = _authorized_request(
        api_url,
        api_token,
        "PATCH",
        f"/projects/{cleaned_project_id}",
        json_body=body,
        expected_statuses={200},
    )
    return _project_view(payload)


def command_edit_folder(
    config: RuntimeConfig,
    *,
    folder_id: str,
    name: str | None,
    description: str | None,
    clear_description: bool,
    parent: str | None,
    clear_parent: bool,
) -> dict[str, Any]:
    api_url = _require_api_url(config)
    api_token = _require_api_token(config)
    cleaned_folder_id = _clean_required_id(folder_id, "folder_id")
    body = _build_edit_body(
        command_name="edit_folder",
        name=name,
        description=description,
        clear_description=clear_description,
        nullable_link_value=parent,
        clear_nullable_link=clear_parent,
        nullable_link_field="parent",
        nullable_link_arg="--parent",
        clear_nullable_link_arg="--clear-parent",
    )

    payload = _authorized_request(
        api_url,
        api_token,
        "PATCH",
        f"/workspaces/folders/{cleaned_folder_id}",
        json_body=body,
        expected_statuses={200},
    )
    return _folder_view(payload)


def dl_transcript(config: RuntimeConfig, *, transcript_id: str) -> dict[str, Any]:
    api_url = _require_api_url(config)
    api_token = _require_api_token(config)
    cleaned_transcript_id = _clean_required_id(transcript_id, "transcript_id")
    captions = _authorized_get_list_of_objects(
        api_url,
        api_token,
        f"/transcripts/{cleaned_transcript_id}/captions",
    )
    return {
        "transcriptId": cleaned_transcript_id,
        "items": captions,
        "count": len(captions),
    }
