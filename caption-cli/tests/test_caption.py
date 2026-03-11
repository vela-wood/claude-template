from __future__ import annotations

import json
from pathlib import Path

import pytest

import caption_cli.cli as cli
import caption_cli.commands as commands
import caption_cli.core as core


class FakeAuthError(Exception):
    def __init__(self) -> None:
        self.status_code = 401
        self.code = "invalid_api_key"
        self.message = "invalid_api_key"
        super().__init__(self.message)


@pytest.fixture
def config(tmp_path: Path) -> core.RuntimeConfig:
    return core.RuntimeConfig(
        api_url="http://localhost:8000",
        api_token="api-token",
        meili_url="https://configured.meili",
        cache_path=tmp_path / "search-token.json",
        output="json",
    )


def write_cache(path: Path, token: str = "cached-token", url: str = "https://cached.meili") -> None:
    path.write_text(json.dumps({"token": token, "url": url}), encoding="utf-8")


def set_runtime_env(monkeypatch: pytest.MonkeyPatch, *, meili_url: str | None = "https://configured.meili") -> None:
    monkeypatch.setenv("CAPTION_API_URL", "http://localhost:8000")
    monkeypatch.setenv("CAPTION_TOKEN", "api-token")
    if meili_url is None:
        monkeypatch.delenv("CAPTION_MEILI_URL", raising=False)
    else:
        monkeypatch.setenv("CAPTION_MEILI_URL", meili_url)


def install_emit_output_capture(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    emitted: dict[str, object] = {}

    def fake_emit_output(
        value: object,
        output_format: str,
        *,
        command_name: str | None = None,
        search_index: str | None = None,
    ) -> None:
        emitted["value"] = value
        emitted["format"] = output_format
        emitted["command_name"] = command_name
        emitted["search_index"] = search_index

    monkeypatch.setattr(cli, "emit_output", fake_emit_output)
    return emitted


def test_token_command_fetches_and_caches_credentials(monkeypatch: pytest.MonkeyPatch, config: core.RuntimeConfig) -> None:
    expected = core.SearchToken(
        token="meili-token",
        url="https://meili.railway.app",
        expires_at="2099-01-01T00:00:00Z",
    )

    monkeypatch.setattr(commands, "fetch_search_token", lambda api_url, api_token: expected)

    result = commands.command_token(config)
    assert result["token"] == "[REDACTED]"
    assert result["url"] == "https://configured.meili"

    cached = json.loads(config.cache_path.read_text(encoding="utf-8"))
    assert cached == {
        "token": "meili-token",
        "url": "https://meili.railway.app",
        "expiresAt": "2099-01-01T00:00:00Z",
    }


def test_token_command_show_token_returns_raw_value(monkeypatch: pytest.MonkeyPatch, config: core.RuntimeConfig) -> None:
    expected = core.SearchToken(
        token="meili-token",
        url="https://meili.railway.app",
        expires_at="2099-01-01T00:00:00Z",
    )

    monkeypatch.setattr(commands, "fetch_search_token", lambda api_url, api_token: expected)

    result = commands.command_token(config, show_token=True)
    assert result["token"] == "meili-token"


def test_parse_args_defaults_env_file_to_repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    loaded: list[tuple[Path, bool]] = []

    def fake_load_dotenv(*, dotenv_path: Path, override: bool) -> None:
        loaded.append((dotenv_path, override))

    monkeypatch.setattr(cli, "load_dotenv", fake_load_dotenv)

    args = cli.parse_args(["list_projects"])

    assert args.env_file == str(cli.DEFAULT_ENV_FILE)
    assert loaded == [(cli.DEFAULT_ENV_FILE, False)]


def test_parse_args_respects_env_file_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    loaded: list[tuple[Path, bool]] = []
    custom_env = tmp_path / "custom.env"

    def fake_load_dotenv(*, dotenv_path: Path, override: bool) -> None:
        loaded.append((dotenv_path, override))

    monkeypatch.setattr(cli, "load_dotenv", fake_load_dotenv)

    args = cli.parse_args(["--env-file", str(custom_env), "list_projects"])

    assert args.env_file == str(custom_env)
    assert loaded == [(custom_env, False)]


def test_parse_args_ignores_caption_meili_cache_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAPTION_MEILI_CACHE", "/tmp/ignored-search-token.json")

    args = cli.parse_args(["list_projects"])

    assert args.cache_path == core.DEFAULT_CACHE_PATH


def test_search_command_defaults_to_captions_index_and_limit() -> None:
    args = cli.parse_args(["search", "term"])

    assert args.command == "search"
    assert args.query == "term"
    assert args.index == core.DEFAULT_SEARCH_INDEX
    assert args.limit == core.DEFAULT_LIMIT == 5
    assert args.output == "table"


def test_search_command_accepts_positional_query_and_index_uid() -> None:
    args = cli.parse_args(["search", "term", "--index", "projects_v1", "--limit", "7"])

    assert args.command == "search"
    assert args.query == "term"
    assert args.index == "projects_v1"
    assert args.limit == 7


def test_search_help_lists_supported_indices(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["search", "--help"])

    captured = capsys.readouterr()
    assert "workspace_folders_v1" in captured.out
    assert "projects_v1" in captured.out
    assert "transcript_sessions_v1" in captured.out


def test_search_command_rejects_limit_lt_1() -> None:
    with pytest.raises(core.CliError, match="--limit must be >= 1"):
        cli.parse_args(["search", "term", "--limit", "0"])


def test_list_projects_command_is_available() -> None:
    args = cli.parse_args(["list_projects"])
    assert args.command == "list_projects"


def test_list_folders_command_is_available() -> None:
    args = cli.parse_args(["list_folders"])
    assert args.command == "list_folders"


def test_create_project_command_is_available() -> None:
    args = cli.parse_args(["create_project", "My Project", "--description", "Desc", "--workspace-id", "w1"])
    assert args.command == "create_project"
    assert args.name == "My Project"
    assert args.description == "Desc"
    assert args.workspace_id == "w1"


def test_create_folder_command_is_available() -> None:
    args = cli.parse_args(["create_folder", "My Folder", "--description", "Desc", "--parent", "f1", "--workspace-id", "w1"])
    assert args.command == "create_folder"
    assert args.name == "My Folder"
    assert args.description == "Desc"
    assert args.parent == "f1"
    assert args.workspace_id == "w1"


def test_edit_project_command_is_available() -> None:
    args = cli.parse_args(["edit_project", "project-1", "--name", "Renamed", "--clear-folder"])
    assert args.command == "edit_project"
    assert args.project_id == "project-1"
    assert args.name == "Renamed"
    assert args.clear_folder is True


def test_edit_folder_command_is_available() -> None:
    args = cli.parse_args(["edit_folder", "folder-1", "--name", "Renamed", "--clear-parent"])
    assert args.command == "edit_folder"
    assert args.folder_id == "folder-1"
    assert args.name == "Renamed"
    assert args.clear_parent is True


def test_dl_transcript_command_is_available() -> None:
    args = cli.parse_args(["dl_transcript", "transcript-1"])
    assert args.command == "dl_transcript"
    assert args.transcript_id == "transcript-1"
    assert args.output == "md"


def test_command_default_outputs_are_applied() -> None:
    assert cli.parse_args(["search", "term"]).output == "table"
    assert cli.parse_args(["list_projects"]).output == "table"
    assert cli.parse_args(["list_folders"]).output == "table"
    assert cli.parse_args(["create_project", "My Project"]).output == "json"


def test_explicit_output_overrides_dl_transcript_default() -> None:
    args = cli.parse_args(["--output", "json", "dl_transcript", "transcript-1"])
    assert args.output == "json"


def test_removed_global_flags_are_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["--api-url", "http://localhost:8000", "list_projects"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--api-token", "api-token", "list_projects"])
    with pytest.raises(SystemExit):
        cli.parse_args(["--meili-url", "https://configured.meili", "token"])


def test_top_level_help_contains_single_page_cheat_sheet(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["-h"])

    output = capsys.readouterr().out
    assert "Command Cheat Sheet" in output
    assert "CAPTION_API_URL" in output
    assert "CAPTION_TOKEN" in output
    assert "CAPTION_MEILI_URL" in output
    assert "--api-url" not in output
    assert "--api-token" not in output
    assert "--meili-url" not in output
    for command_name in (
        "token",
        "search",
        "list_projects",
        "list_folders",
        "create_project",
        "create_folder",
        "edit_project",
        "edit_folder",
        "dl_transcript",
    ):
        assert command_name in output
    assert "usage: caption search <query> [--index INDEX] [--limit N]" in output
    assert "example: caption --output json dl_transcript <transcript-uuid>" in output


def test_legacy_subcommands_are_removed() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["search-global", "--query", "term"])

    with pytest.raises(SystemExit):
        cli.parse_args(["search-captions", "--query", "term", "--session-id", "abc"])

    with pytest.raises(SystemExit):
        cli.parse_args(["search-folders", "--query", "eng"])

    with pytest.raises(SystemExit):
        cli.parse_args(["create", "name"])

    with pytest.raises(SystemExit):
        cli.parse_args(["edit", "id"])


def test_command_search_uses_index_search_endpoint_shape(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    write_cache(config.cache_path)
    index_calls: list[str] = []
    search_calls: list[tuple[str, dict[str, int]]] = []

    class FakeIndex:
        def search(self, query, opt_params=None):
            search_calls.append((query, dict(opt_params or {})))
            return {"hits": []}

    class FakeClient:
        def index(self, index_uid):
            index_calls.append(index_uid)
            return FakeIndex()

    monkeypatch.setattr(core, "build_meili_client", lambda url, token: FakeClient())

    result = commands.command_search(config, query="roadmap", index="projects_v1", limit=7)

    assert result == {"hits": []}
    assert index_calls == ["projects_v1"]
    assert search_calls == [("roadmap", {"limit": 7})]


def test_command_search_rejects_empty_index(
    config: core.RuntimeConfig,
) -> None:
    with pytest.raises(core.CliError, match="--index cannot be empty"):
        commands.command_search(config, query="term", index="   ", limit=5)


def test_invalid_meili_token_refreshes_once_and_retries(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    write_cache(config.cache_path, token="stale-token", url="https://old.meili")
    built_clients: list[tuple[str, str]] = []
    fetch_calls: list[tuple[str, str]] = []
    index_calls: list[str] = []

    class FirstIndex:
        def search(self, query, opt_params=None):
            raise FakeAuthError()

    class SecondIndex:
        def search(self, query, opt_params=None):
            return {"hits": [{"id": "ok"}]}

    class FirstClient:
        def index(self, index_uid):
            index_calls.append(index_uid)
            return FirstIndex()

    class SecondClient:
        def index(self, index_uid):
            index_calls.append(index_uid)
            return SecondIndex()

    def fake_build_client(url: str, token: str):
        built_clients.append((url, token))
        if token == "stale-token":
            return FirstClient()
        return SecondClient()

    def fake_fetch_search_token(api_url: str, api_token: str) -> core.SearchToken:
        fetch_calls.append((api_url, api_token))
        return core.SearchToken(token="fresh-token", url="https://new.meili")

    monkeypatch.setattr(core, "build_meili_client", fake_build_client)
    monkeypatch.setattr(core, "fetch_search_token", fake_fetch_search_token)

    result = commands.command_search(config, query="retry", index=core.DEFAULT_SEARCH_INDEX, limit=2)

    assert result == {"hits": [{"id": "ok"}]}
    assert fetch_calls == [("http://localhost:8000", "api-token")]
    assert built_clients == [
        ("https://configured.meili", "stale-token"),
        ("https://configured.meili", "fresh-token"),
    ]
    assert index_calls == [core.DEFAULT_SEARCH_INDEX, core.DEFAULT_SEARCH_INDEX]

    cached = json.loads(config.cache_path.read_text(encoding="utf-8"))
    assert cached == {"token": "fresh-token", "url": "https://new.meili"}


def test_run_search_uses_default_index_when_flag_omitted(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    set_runtime_env(monkeypatch, meili_url=config.meili_url)
    write_cache(config.cache_path)
    index_calls: list[str] = []

    class FakeIndex:
        def search(self, query, opt_params=None):
            return {"hits": [{"query": query, "limit": opt_params["limit"]}]}

    class FakeClient:
        def index(self, index_uid):
            index_calls.append(index_uid)
            return FakeIndex()

    monkeypatch.setattr(core, "build_meili_client", lambda url, token: FakeClient())
    emitted = install_emit_output_capture(monkeypatch)

    exit_code = cli.run(
        [
            "--env-file",
            "",
            "--cache-path",
            str(config.cache_path),
            "search",
            "term",
        ]
    )

    assert exit_code == 0
    assert index_calls == [core.DEFAULT_SEARCH_INDEX]
    assert emitted["format"] == "table"
    assert emitted["value"] == {"hits": [{"query": "term", "limit": 5}]}
    assert emitted["command_name"] == "search"
    assert emitted["search_index"] == core.DEFAULT_SEARCH_INDEX


def test_command_list_projects_fetches_workspace_and_all_projects(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    page_calls: list[tuple[str, str, int, int]] = []

    def fake_fetch_current_workspace_id(api_url: str, api_token: str) -> str:
        assert api_url == "http://localhost:8000"
        assert api_token == "api-token"
        return "workspace-uuid"

    def fake_fetch_workspace_items_page(
        api_url: str,
        api_token: str,
        workspace_id: str,
        endpoint: str,
        *,
        page: int,
        limit: int,
    ) -> list[dict[str, object]]:
        page_calls.append((workspace_id, endpoint, page, limit))
        assert endpoint == "projects"
        return [
            {
                "id": "p1",
                "createdAt": "2023-12-31T00:00:00Z",
                "updatedAt": "2024-01-01T00:00:00Z",
                "name": "Alpha",
                "description": "First",
                "folder": None,
                "transcript": "t1",
                "workspace": "w1",
                "createdBy": "u1",
            },
            {
                "id": "p2",
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-02T00:00:00Z",
                "name": "Beta",
                "description": None,
                "folder": "f1",
                "transcript": "t2",
                "workspace": "w1",
                "updatedBy": "u2",
            },
        ]

    monkeypatch.setattr(commands, "fetch_current_workspace_id", fake_fetch_current_workspace_id)
    monkeypatch.setattr(commands, "fetch_workspace_items_page", fake_fetch_workspace_items_page)

    result = commands.command_list_projects(config)

    assert result["workspaceId"] == "workspace-uuid"
    assert result["count"] == 2
    assert result["totalCount"] == 2
    assert result["totalPages"] == 1
    assert [item["id"] for item in result["items"]] == ["p1", "p2"]
    for field in ("id", "createdAt", "updatedAt", "name", "description", "folder", "transcript"):
        assert field in result["items"][0]
    assert "workspace" not in result["items"][0]
    assert result["items"][0]["transcript"] == "t1"
    assert result["items"][1]["transcript"] == "t2"
    assert page_calls == [
        ("workspace-uuid", "projects", 0, core.WORKSPACE_LIST_PAGE_SIZE),
    ]


def test_command_list_folders_fetches_workspace_and_all_folders(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    page_calls: list[tuple[str, str, int, int]] = []

    def fake_fetch_current_workspace_id(api_url: str, api_token: str) -> str:
        assert api_url == "http://localhost:8000"
        assert api_token == "api-token"
        return "workspace-uuid"

    def fake_fetch_workspace_items_page(
        api_url: str,
        api_token: str,
        workspace_id: str,
        endpoint: str,
        *,
        page: int,
        limit: int,
    ) -> list[dict[str, object]]:
        page_calls.append((workspace_id, endpoint, page, limit))
        assert endpoint == "folders"
        return [
            {
                "id": "f1",
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-02T00:00:00Z",
                "name": "Alpha Folder",
                "description": "Top level",
                "parent": None,
                "workspace": "w1",
                "createdBy": "u1",
            },
            {
                "id": "f2",
                "createdAt": "2024-01-03T00:00:00Z",
                "updatedAt": "2024-01-04T00:00:00Z",
                "name": "Child Folder",
                "description": None,
                "parent": "f1",
                "workspace": "w1",
                "updatedBy": "u2",
            },
        ]

    monkeypatch.setattr(commands, "fetch_current_workspace_id", fake_fetch_current_workspace_id)
    monkeypatch.setattr(commands, "fetch_workspace_items_page", fake_fetch_workspace_items_page)

    result = commands.command_list_folders(config)

    assert result["workspaceId"] == "workspace-uuid"
    assert result["count"] == 2
    assert result["totalCount"] == 2
    assert result["totalPages"] == 1
    assert [item["id"] for item in result["items"]] == ["f1", "f2"]
    for field in ("id", "createdAt", "updatedAt", "name", "description", "parent"):
        assert field in result["items"][0]
    assert "workspace" not in result["items"][0]
    assert result["items"][0]["parent"] is None
    assert result["items"][1]["parent"] == "f1"
    assert page_calls == [
        ("workspace-uuid", "folders", 0, core.WORKSPACE_LIST_PAGE_SIZE),
    ]


def test_command_create_project_uses_workspace_lookup_and_returns_filtered_project(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    request_calls: list[tuple[str, str, str, str, object, object]] = []

    def fake_fetch_current_workspace_id(api_url: str, api_token: str) -> str:
        assert api_url == "http://localhost:8000"
        assert api_token == "api-token"
        return "workspace-uuid"

    def fake_authorized_request(
        api_url: str,
        api_token: str,
        method: str,
        path: str,
        params=None,
        json_body=None,
        expected_statuses=None,
    ) -> dict[str, object]:
        request_calls.append((api_url, api_token, method, path, json_body, expected_statuses))
        return {
            "id": "p1",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-02T00:00:00Z",
            "name": "My Project",
            "description": "Desc",
            "folder": None,
            "transcript": "t1",
            "workspace": "workspace-uuid",
            "createdBy": "u1",
        }

    monkeypatch.setattr(commands, "fetch_current_workspace_id", fake_fetch_current_workspace_id)
    monkeypatch.setattr(commands, "_authorized_request", fake_authorized_request)

    result = commands.command_create_project(config, name="My Project", description="Desc", workspace_id=None)

    assert request_calls == [
        (
            "http://localhost:8000",
            "api-token",
            "POST",
            "/folders/workspace-uuid/projects",
            {"name": "My Project", "description": "Desc"},
            {201},
        )
    ]
    assert result == {
        "id": "p1",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
        "name": "My Project",
        "description": "Desc",
        "folder": None,
        "transcript": "t1",
    }


def test_command_create_project_uses_explicit_workspace_id(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    paths: list[str] = []

    def fake_authorized_request(
        api_url: str,
        api_token: str,
        method: str,
        path: str,
        params=None,
        json_body=None,
        expected_statuses=None,
    ) -> dict[str, object]:
        paths.append(path)
        return {
            "id": "p2",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:00Z",
            "name": "Explicit",
            "description": None,
            "folder": None,
            "transcript": "t2",
        }

    monkeypatch.setattr(commands, "_authorized_request", fake_authorized_request)

    commands.command_create_project(config, name="Explicit", description=None, workspace_id="workspace-explicit")
    assert paths == ["/folders/workspace-explicit/projects"]


def test_command_create_folder_uses_folder_endpoint_and_returns_filtered_folder(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    request_calls: list[tuple[str, str, str, str, object, object]] = []

    def fake_fetch_current_workspace_id(api_url: str, api_token: str) -> str:
        assert api_url == "http://localhost:8000"
        assert api_token == "api-token"
        return "workspace-uuid"

    def fake_authorized_request(
        api_url: str,
        api_token: str,
        method: str,
        path: str,
        params=None,
        json_body=None,
        expected_statuses=None,
    ) -> dict[str, object]:
        request_calls.append((api_url, api_token, method, path, json_body, expected_statuses))
        return {
            "id": "f1",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-02T00:00:00Z",
            "name": "My Folder",
            "description": "Desc",
            "parent": "parent-uuid",
            "workspace": "workspace-uuid",
            "createdBy": "u1",
        }

    monkeypatch.setattr(commands, "fetch_current_workspace_id", fake_fetch_current_workspace_id)
    monkeypatch.setattr(commands, "_authorized_request", fake_authorized_request)

    result = commands.command_create_folder(
        config,
        name="My Folder",
        description="Desc",
        parent="parent-uuid",
        workspace_id=None,
    )

    assert request_calls == [
        (
            "http://localhost:8000",
            "api-token",
            "POST",
            "/folders/workspace-uuid/folders",
            {"name": "My Folder", "description": "Desc", "parent": "parent-uuid"},
            {200, 201},
        )
    ]
    assert result == {
        "id": "f1",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-02T00:00:00Z",
        "name": "My Folder",
        "description": "Desc",
        "parent": "parent-uuid",
    }


def test_command_edit_project_patches_project_and_returns_filtered_fields(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    request_calls: list[tuple[str, str, str, str, object, object]] = []

    def fake_authorized_request(
        api_url: str,
        api_token: str,
        method: str,
        path: str,
        params=None,
        json_body=None,
        expected_statuses=None,
    ) -> dict[str, object]:
        request_calls.append((api_url, api_token, method, path, json_body, expected_statuses))
        return {
            "id": "p1",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-03T00:00:00Z",
            "name": "Renamed",
            "description": None,
            "folder": None,
            "transcript": "t1",
            "workspace": "workspace-uuid",
        }

    monkeypatch.setattr(commands, "_authorized_request", fake_authorized_request)

    result = commands.command_edit_project(
        config,
        project_id="project-uuid",
        name="Renamed",
        description=None,
        clear_description=True,
        folder=None,
        clear_folder=True,
    )

    assert request_calls == [
        (
            "http://localhost:8000",
            "api-token",
            "PATCH",
            "/projects/project-uuid",
            {"name": "Renamed", "description": None, "folder": None},
            {200},
        )
    ]
    assert result == {
        "id": "p1",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-03T00:00:00Z",
        "name": "Renamed",
        "description": None,
        "folder": None,
        "transcript": "t1",
    }


def test_command_edit_folder_patches_folder_and_returns_filtered_fields(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    request_calls: list[tuple[str, str, str, str, object, object]] = []

    def fake_authorized_request(
        api_url: str,
        api_token: str,
        method: str,
        path: str,
        params=None,
        json_body=None,
        expected_statuses=None,
    ) -> dict[str, object]:
        request_calls.append((api_url, api_token, method, path, json_body, expected_statuses))
        return {
            "id": "f1",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-03T00:00:00Z",
            "name": "Renamed Folder",
            "description": None,
            "parent": None,
            "workspace": "workspace-uuid",
        }

    monkeypatch.setattr(commands, "_authorized_request", fake_authorized_request)

    result = commands.command_edit_folder(
        config,
        folder_id="folder-uuid",
        name="Renamed Folder",
        description=None,
        clear_description=True,
        parent=None,
        clear_parent=True,
    )

    assert request_calls == [
        (
            "http://localhost:8000",
            "api-token",
            "PATCH",
            "/folders/folder-uuid",
            {"name": "Renamed Folder", "description": None, "parent": None},
            {200},
        )
    ]
    assert result == {
        "id": "f1",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-03T00:00:00Z",
        "name": "Renamed Folder",
        "description": None,
        "parent": None,
    }


def test_dl_transcript_fetches_captions_for_transcript(
    monkeypatch: pytest.MonkeyPatch,
    config: core.RuntimeConfig,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def fake_authorized_get_list_of_objects(
        api_url: str,
        api_token: str,
        path: str,
    ) -> list[dict[str, object]]:
        calls.append((api_url, api_token, path))
        return [
            {
                "id": "c1",
                "createdAt": "2024-01-01T00:00:00Z",
                "updatedAt": "2024-01-01T00:00:01Z",
                "session": "s1",
                "speaker": None,
                "channel": 0,
                "index": 0,
                "content": "First line",
            },
            {
                "id": "c2",
                "createdAt": "2024-01-01T00:00:02Z",
                "updatedAt": "2024-01-01T00:00:03Z",
                "session": "s1",
                "speaker": "speaker-1",
                "channel": 1,
                "index": 1,
                "content": "Second line",
            },
        ]

    monkeypatch.setattr(commands, "_authorized_get_list_of_objects", fake_authorized_get_list_of_objects)

    result = commands.dl_transcript(config, transcript_id="transcript-uuid")

    assert calls == [
        ("http://localhost:8000", "api-token", "/transcripts/transcript-uuid/captions"),
    ]
    assert result["transcriptId"] == "transcript-uuid"
    assert result["count"] == 2
    assert [item["id"] for item in result["items"]] == ["c1", "c2"]


def test_transcript_items_to_md_merges_consecutive_speaker_lines() -> None:
    transcript_items = [
        {"createdAt": "2025-12-18T15:01:23Z", "channel": 0, "index": 0, "content": "think bryce"},
        {"createdAt": "2025-12-18T15:01:23.400Z", "channel": 1, "index": 0, "content": "hear me"},
        {"createdAt": "2025-12-18T15:01:25Z", "channel": 0, "index": 0, "content": "is gonna"},
        {"createdAt": "2025-12-18T15:01:25.200Z", "channel": 0, "index": 0, "content": "blank room now"},
        {"createdAt": "2025-12-18T15:01:30Z", "channel": 1, "index": 0, "content": "oh for the yeah"},
    ]

    output = core._transcript_items_to_md(transcript_items)

    assert output == "\n".join(
        [
            "[15:01.23] me: think bryce",
            "[15:01.23] meeting-0: hear me",
            "[15:01.25] me: is gonna blank room now",
            "[15:01.30] meeting-0: oh for the yeah",
        ]
    )


def test_emit_output_search_table_for_transcript_captions_uses_condensed_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "hits": [
            {
                "id": "caption-hit-id",
                "updatedAt": "2026-02-09T18:41:33.097Z",
                "format": 1.1,
                "scope": {"workspaceId": "w-uuid", "folderIds": [], "projectId": "project-uuid-1"},
                "sessionId": "session-uuid",
                "speaker": {"id": "speaker-uuid-1", "name": "0:0"},
                "content": "patent work before and a lot of the money",
                "createdAt": 1766079084978,
            }
        ],
        "estimatedTotalHits": 1,
    }

    core.emit_output(payload, "table", command_name="search", search_index="transcript_captions_v1")
    out = capsys.readouterr().out

    assert "estimatedTotalHits: 1 | returned: 1" in out
    assert "| # | scope.projectId (uuid) | speaker.name | updatedAt (YYYYMMDD) | content |" in out
    assert "project-uuid-1" in out
    assert "20260209" in out
    assert "speaker.id" not in out
    assert "sessionId" not in out
    assert "createdAt" not in out


def test_emit_output_search_table_for_projects_uses_condensed_columns(
    capsys: pytest.CaptureFixture[str],
) -> None:
    payload = {
        "hits": [
            {
                "id": "project-uuid-1",
                "updatedAt": "2026-02-09T18:41:33.097Z",
                "format": 1,
                "scope": {"workspaceId": "workspace-uuid", "folderIds": []},
                "name": "Transcript 2026-02-09",
                "description": None,
            }
        ],
        "estimatedTotalHits": 1,
    }

    core.emit_output(payload, "table", command_name="search", search_index="projects_v1")
    out = capsys.readouterr().out

    assert "estimatedTotalHits: 1 | returned: 1" in out
    assert "| # | id (project uuid) | updatedAt (YYYYMMDD) | name | description |" in out
    assert "project-uuid-1" in out
    assert "20260209" in out
    assert "scope" not in out
    assert "format" not in out


def test_transcript_items_to_md_rejects_missing_channel() -> None:
    with pytest.raises(core.CliError, match="Transcript item 0 missing integer 'channel'"):
        core._transcript_items_to_md(
            [{"createdAt": "2025-12-18T15:01:23Z", "index": 0, "content": "hello"}]
        )


def test_command_edit_project_requires_update_fields(config: core.RuntimeConfig) -> None:
    with pytest.raises(core.CliError, match="edit_project requires at least one field"):
        commands.command_edit_project(
            config,
            project_id="project-uuid",
            name=None,
            description=None,
            clear_description=False,
            folder=None,
            clear_folder=False,
        )


def test_command_edit_project_rejects_conflicting_nullable_flags(config: core.RuntimeConfig) -> None:
    with pytest.raises(core.CliError, match="Use either --description or --clear-description"):
        commands.command_edit_project(
            config,
            project_id="project-uuid",
            name=None,
            description="new",
            clear_description=True,
            folder=None,
            clear_folder=False,
        )

    with pytest.raises(core.CliError, match="Use either --folder or --clear-folder"):
        commands.command_edit_project(
            config,
            project_id="project-uuid",
            name=None,
            description=None,
            clear_description=False,
            folder="folder-uuid",
            clear_folder=True,
        )


def test_command_edit_folder_rejects_conflicting_nullable_flags(config: core.RuntimeConfig) -> None:
    with pytest.raises(core.CliError, match="Use either --description or --clear-description"):
        commands.command_edit_folder(
            config,
            folder_id="folder-uuid",
            name=None,
            description="new",
            clear_description=True,
            parent=None,
            clear_parent=False,
        )

    with pytest.raises(core.CliError, match="Use either --parent or --clear-parent"):
        commands.command_edit_folder(
            config,
            folder_id="folder-uuid",
            name=None,
            description=None,
            clear_description=False,
            parent="parent-uuid",
            clear_parent=True,
        )


def test_run_list_projects_does_not_require_meili_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_runtime_env(monkeypatch, meili_url=None)
    emitted = install_emit_output_capture(monkeypatch)

    def fake_command_list_projects(config: core.RuntimeConfig) -> dict[str, object]:
        return {
            "workspaceId": "workspace-uuid",
            "items": [{"id": "p1", "name": "Alpha", "updatedAt": "2024-01-01T00:00:00Z", "folder": None}],
            "count": 1,
            "totalCount": 1,
            "totalPages": 1,
        }

    monkeypatch.setattr(cli, "command_list_projects", fake_command_list_projects)

    exit_code = cli.run(
        [
            "--env-file",
            "",
            "--cache-path",
            str(tmp_path / "search-token.json"),
            "list_projects",
        ]
    )

    assert exit_code == 0
    assert emitted["format"] == "table"
    assert emitted["value"] == {
        "workspaceId": "workspace-uuid",
        "items": [{"id": "p1", "name": "Alpha", "updatedAt": "2024-01-01T00:00:00Z", "folder": None}],
        "count": 1,
        "totalCount": 1,
        "totalPages": 1,
    }


def test_run_list_folders_does_not_require_meili_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_runtime_env(monkeypatch, meili_url=None)
    emitted = install_emit_output_capture(monkeypatch)

    def fake_command_list_folders(config: core.RuntimeConfig) -> dict[str, object]:
        return {
            "workspaceId": "workspace-uuid",
            "items": [{"id": "f1", "name": "Root", "updatedAt": "2024-01-01T00:00:00Z", "parent": None}],
            "count": 1,
            "totalCount": 1,
            "totalPages": 1,
        }

    monkeypatch.setattr(cli, "command_list_folders", fake_command_list_folders)

    exit_code = cli.run(
        [
            "--env-file",
            "",
            "--cache-path",
            str(tmp_path / "search-token.json"),
            "list_folders",
        ]
    )

    assert exit_code == 0
    assert emitted["format"] == "table"
    assert emitted["value"] == {
        "workspaceId": "workspace-uuid",
        "items": [{"id": "f1", "name": "Root", "updatedAt": "2024-01-01T00:00:00Z", "parent": None}],
        "count": 1,
        "totalCount": 1,
        "totalPages": 1,
    }


def test_run_dl_transcript_does_not_require_meili_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_runtime_env(monkeypatch, meili_url=None)

    def fake_dl_transcript(config: core.RuntimeConfig, *, transcript_id: str) -> dict[str, object]:
        assert transcript_id == "transcript-uuid"
        return {
            "transcriptId": transcript_id,
            "items": [
                {
                    "id": "c1",
                    "createdAt": "2025-12-18T15:01:23Z",
                    "channel": 0,
                    "index": 0,
                    "content": "hello",
                },
                {
                    "id": "c2",
                    "createdAt": "2025-12-18T15:01:23.400Z",
                    "channel": 0,
                    "index": 0,
                    "content": "there",
                },
                {
                    "id": "c3",
                    "createdAt": "2025-12-18T15:01:24Z",
                    "channel": 1,
                    "index": 0,
                    "content": "hi",
                },
            ],
            "count": 3,
        }

    monkeypatch.setattr(cli, "dl_transcript", fake_dl_transcript)

    exit_code = cli.run(
        [
            "--env-file",
            "",
            "--cache-path",
            str(tmp_path / "search-token.json"),
            "dl_transcript",
            "transcript-uuid",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.out == "[15:01.23] me: hello there\n[15:01.24] meeting-0: hi\n"


def test_run_dl_transcript_with_json_output_emits_raw_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_runtime_env(monkeypatch, meili_url=None)

    payload = {
        "transcriptId": "transcript-uuid",
        "items": [
            {
                "id": "c1",
                "createdAt": "2025-12-18T15:01:23Z",
                "channel": 0,
                "index": 0,
                "content": "hello",
            }
        ],
        "count": 1,
    }

    def fake_dl_transcript(config: core.RuntimeConfig, *, transcript_id: str) -> dict[str, object]:
        assert transcript_id == "transcript-uuid"
        return payload

    monkeypatch.setattr(cli, "dl_transcript", fake_dl_transcript)

    exit_code = cli.run(
        [
            "--env-file",
            "",
            "--cache-path",
            str(tmp_path / "search-token.json"),
            "--output",
            "json",
            "dl_transcript",
            "transcript-uuid",
        ]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == payload


def test_run_create_project_does_not_require_meili_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_runtime_env(monkeypatch, meili_url=None)
    emitted = install_emit_output_capture(monkeypatch)

    def fake_command_create_project(
        config: core.RuntimeConfig,
        *,
        name: str,
        description: str | None,
        workspace_id: str | None,
    ) -> dict[str, object]:
        assert name == "My Project"
        assert description == "Desc"
        assert workspace_id is None
        return {
            "id": "p1",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:00Z",
            "name": "My Project",
            "description": "Desc",
            "folder": None,
            "transcript": "t1",
        }

    monkeypatch.setattr(cli, "command_create_project", fake_command_create_project)

    exit_code = cli.run(
        [
            "--env-file",
            "",
            "--cache-path",
            str(tmp_path / "search-token.json"),
            "create_project",
            "My Project",
            "--description",
            "Desc",
        ]
    )

    assert exit_code == 0
    assert emitted["format"] == "json"
    assert emitted["value"] == {
        "id": "p1",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z",
        "name": "My Project",
        "description": "Desc",
        "folder": None,
        "transcript": "t1",
    }


def test_run_create_folder_does_not_require_meili_url(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    set_runtime_env(monkeypatch, meili_url=None)
    emitted = install_emit_output_capture(monkeypatch)

    def fake_command_create_folder(
        config: core.RuntimeConfig,
        *,
        name: str,
        description: str | None,
        parent: str | None,
        workspace_id: str | None,
    ) -> dict[str, object]:
        assert name == "My Folder"
        assert description == "Desc"
        assert parent == "parent-uuid"
        assert workspace_id is None
        return {
            "id": "f1",
            "createdAt": "2024-01-01T00:00:00Z",
            "updatedAt": "2024-01-01T00:00:00Z",
            "name": "My Folder",
            "description": "Desc",
            "parent": "parent-uuid",
        }

    monkeypatch.setattr(cli, "command_create_folder", fake_command_create_folder)

    exit_code = cli.run(
        [
            "--env-file",
            "",
            "--cache-path",
            str(tmp_path / "search-token.json"),
            "create_folder",
            "My Folder",
            "--description",
            "Desc",
            "--parent",
            "parent-uuid",
        ]
    )

    assert exit_code == 0
    assert emitted["format"] == "json"
    assert emitted["value"] == {
        "id": "f1",
        "createdAt": "2024-01-01T00:00:00Z",
        "updatedAt": "2024-01-01T00:00:00Z",
        "name": "My Folder",
        "description": "Desc",
        "parent": "parent-uuid",
    }


def test_run_fails_when_api_url_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPTION_API_URL", raising=False)
    monkeypatch.setenv("CAPTION_TOKEN", "api-token")
    monkeypatch.setenv("CAPTION_MEILI_URL", "https://configured.meili")

    with pytest.raises(core.CliError, match="Missing Caption API URL"):
        cli.run(["--env-file", "", "token"])


def test_run_fails_when_meili_url_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAPTION_MEILI_URL", raising=False)
    monkeypatch.setenv("CAPTION_API_URL", "http://localhost:8000")
    monkeypatch.setenv("CAPTION_TOKEN", "api-token")

    with pytest.raises(core.CliError, match="Missing Meilisearch URL"):
        cli.run(["--env-file", "", "token"])
