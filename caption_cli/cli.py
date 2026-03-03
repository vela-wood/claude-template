from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Sequence

from dotenv import load_dotenv

from caption_cli.commands import (
    command_create_folder,
    command_create_project,
    command_edit_folder,
    command_edit_project,
    command_list_folders,
    command_list_projects,
    command_search,
    command_token,
    dl_transcript,
)
from caption_cli.core import (
    CliError,
    CommandSpec,
    DEFAULT_CACHE_PATH,
    DEFAULT_LIMIT,
    DEFAULT_SEARCH_INDEX,
    RuntimeConfig,
    _require_api_url,
    _require_meili_url,
    emit_output,
)

DEFAULT_ENV_FILE = ".env"


def _top_level_help_epilog(specs: Sequence[CommandSpec]) -> str:
    lines = [
        "Environment",
        "  CAPTION_API_URL   required for all commands",
        "  CAPTION_TOKEN     required for authenticated API calls",
        "  CAPTION_MEILI_URL required for token and search",
        "",
        "Global options",
        "  --env-file ENV_FILE      dotenv file loaded before env resolution (default: <repo-root>/.env)",
        "  --cache-path CACHE_PATH  search token cache path (default: search-token.json)",
        "  --output {json,table,md} output format (default: json, except search=table and dl_transcript=md)",
        "",
        "Command Cheat Sheet",
    ]
    for spec in specs:
        lines.extend(
            [
                "",
                f"{spec.name}",
                f"  usage: {spec.usage}",
            ]
        )
        if spec.notes:
            lines.append("  notes:")
            for note in spec.notes:
                lines.append(f"    - {note}")
        lines.append(f"  example: {spec.example}")
    return "\n".join(lines)


def build_parser() -> tuple[argparse.ArgumentParser, dict[str, CommandSpec]]:
    specs = tuple(_command_specs())
    parser = argparse.ArgumentParser(
        prog="caption",
        description="Caption CLI for API and Meilisearch operations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_top_level_help_epilog(specs),
        allow_abbrev=False,
    )
    parser.add_argument(
        "--cache-path",
        default=os.getenv("CAPTION_MEILI_CACHE", str(DEFAULT_CACHE_PATH)),
    )
    parser.add_argument(
        "--output",
        choices=("json", "table", "md"),
        default=None,
    )
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="dotenv path loaded before env-based defaults are resolved",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)
    for spec in specs:
        subparser = subparsers.add_parser(spec.name, help=spec.help, allow_abbrev=False)
        spec.add_arguments(subparser)

    return parser, {spec.name: spec for spec in specs}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    pre_parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    pre_args, _ = pre_parser.parse_known_args(argv)
    if pre_args.env_file:
        load_dotenv(dotenv_path=Path(pre_args.env_file), override=False)

    parser, command_specs = build_parser()
    args = parser.parse_args(argv)
    if args.output is None:
        args.output = command_specs[args.command].default_output
    if hasattr(args, "limit") and args.limit < 1:
        raise CliError("--limit must be >= 1")

    return args


def _add_no_arguments(_: argparse.ArgumentParser) -> None:
    return None


def _add_token_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--show-token",
        action="store_true",
        help="Print the raw Meili search token in output (sensitive)",
    )


def _add_search_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", help="Search query string")
    parser.add_argument(
        "--index",
        default=DEFAULT_SEARCH_INDEX,
        help=(
            "Index UID (for example: transcript_captions_v1, workspace_folders_v1, "
            "projects_v1, transcript_sessions_v1)."
        ),
    )
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help=f"Maximum results (default: {DEFAULT_LIMIT})")


def _add_create_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", help="Project name")
    parser.add_argument("--description", help="Project description", default=None)
    parser.add_argument(
        "--workspace-id",
        default=None,
        help="Workspace UUID. If omitted, resolves from /users/me/workspace.",
    )


def _add_create_folder_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("name", help="Folder name")
    parser.add_argument("--description", help="Folder description", default=None)
    parser.add_argument("--parent", default=None, help="Parent folder UUID")
    parser.add_argument(
        "--workspace-id",
        default=None,
        help="Workspace UUID. If omitted, resolves from /users/me/workspace.",
    )


def _add_edit_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("project_id", help="Project UUID")
    parser.add_argument("--name", default=None, help="New project name")
    parser.add_argument("--description", default=None, help="New project description")
    parser.add_argument(
        "--clear-description",
        action="store_true",
        help="Set description to null",
    )
    parser.add_argument("--folder", default=None, help="Folder UUID")
    parser.add_argument(
        "--clear-folder",
        action="store_true",
        help="Set folder to null",
    )


def _add_edit_folder_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("folder_id", help="Folder UUID")
    parser.add_argument("--name", default=None, help="New folder name")
    parser.add_argument("--description", default=None, help="New folder description")
    parser.add_argument(
        "--clear-description",
        action="store_true",
        help="Set description to null",
    )
    parser.add_argument("--parent", default=None, help="Parent folder UUID")
    parser.add_argument(
        "--clear-parent",
        action="store_true",
        help="Set parent to null",
    )


def _add_dl_transcript_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("transcript_id", help="Transcript UUID")


def _handle_token(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]:
    return command_token(config, show_token=args.show_token)


def _handle_search(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]:
    return command_search(config, query=args.query, index=args.index, limit=args.limit)


def _handle_list_projects(config: RuntimeConfig, _: argparse.Namespace) -> dict[str, Any]:
    return command_list_projects(config)


def _handle_list_folders(config: RuntimeConfig, _: argparse.Namespace) -> dict[str, Any]:
    return command_list_folders(config)


def _handle_create_project(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]:
    return command_create_project(
        config,
        name=args.name,
        description=args.description,
        workspace_id=args.workspace_id,
    )


def _handle_create_folder(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]:
    return command_create_folder(
        config,
        name=args.name,
        description=args.description,
        parent=args.parent,
        workspace_id=args.workspace_id,
    )


def _handle_edit_project(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]:
    return command_edit_project(
        config,
        project_id=args.project_id,
        name=args.name,
        description=args.description,
        clear_description=args.clear_description,
        folder=args.folder,
        clear_folder=args.clear_folder,
    )


def _handle_edit_folder(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]:
    return command_edit_folder(
        config,
        folder_id=args.folder_id,
        name=args.name,
        description=args.description,
        clear_description=args.clear_description,
        parent=args.parent,
        clear_parent=args.clear_parent,
    )


def _handle_dl_transcript(config: RuntimeConfig, args: argparse.Namespace) -> dict[str, Any]:
    return dl_transcript(config, transcript_id=args.transcript_id)


def _command_specs() -> Sequence[CommandSpec]:
    return (
        CommandSpec(
            name="token",
            help="Fetch and cache /search/token credentials",
            add_arguments=_add_token_arguments,
            handler=_handle_token,
            needs_meili=True,
            usage="caption token [--show-token]",
            notes=(
                "Fetches Meilisearch credentials from GET {CAPTION_API_URL}/search/token.",
                "Writes token payload to --cache-path.",
                "Output redacts token by default; use --show-token only when explicitly needed.",
            ),
            example="caption token --show-token",
        ),
        CommandSpec(
            name="search",
            help="Search one Meilisearch index",
            add_arguments=_add_search_arguments,
            handler=_handle_search,
            needs_meili=True,
            default_output="table",
            usage="caption search <query> [--index INDEX] [--limit N]",
            notes=(
                "--limit must be >= 1.",
                "Uses cached token and refreshes once on Meili auth failures.",
            ),
            example="caption search \"roadmap\" --index projects_v1 --limit 10",
        ),
        CommandSpec(
            name="list_projects",
            help="List all projects in the current user's workspace",
            add_arguments=_add_no_arguments,
            handler=_handle_list_projects,
            usage="caption list_projects",
            notes=("Fetches workspace via /users/me/workspace and paginates projects.",),
            example="caption list_projects",
        ),
        CommandSpec(
            name="list_folders",
            help="List all folders in the current user's workspace",
            add_arguments=_add_no_arguments,
            handler=_handle_list_folders,
            usage="caption list_folders",
            notes=("Fetches workspace via /users/me/workspace and paginates folders.",),
            example="caption list_folders",
        ),
        CommandSpec(
            name="create_project",
            help="Create a new project in a workspace",
            add_arguments=_add_create_project_arguments,
            handler=_handle_create_project,
            usage="caption create_project <name> [--description TEXT] [--workspace-id UUID]",
            notes=("If --workspace-id is omitted, workspace ID is resolved from /users/me/workspace.",),
            example="caption create_project \"My Project\" --description \"First draft\"",
        ),
        CommandSpec(
            name="create_folder",
            help="Create a new folder in a workspace",
            add_arguments=_add_create_folder_arguments,
            handler=_handle_create_folder,
            usage="caption create_folder <name> [--description TEXT] [--parent UUID] [--workspace-id UUID]",
            notes=("If --workspace-id is omitted, workspace ID is resolved from /users/me/workspace.",),
            example="caption create_folder \"My Folder\" --parent <parent-folder-uuid>",
        ),
        CommandSpec(
            name="edit_project",
            help="Edit a project via PATCH /projects/{projectId}",
            add_arguments=_add_edit_project_arguments,
            handler=_handle_edit_project,
            usage=(
                "caption edit_project <project_id> "
                "[--name TEXT] [--description TEXT|--clear-description] [--folder UUID|--clear-folder]"
            ),
            notes=(
                "At least one field is required.",
                "Conflicting nullable pairs are rejected.",
            ),
            example="caption edit_project <project-uuid> --name \"Renamed\" --clear-folder",
        ),
        CommandSpec(
            name="edit_folder",
            help="Edit a folder via PATCH /workspaces/folders/{folderId}",
            add_arguments=_add_edit_folder_arguments,
            handler=_handle_edit_folder,
            usage=(
                "caption edit_folder <folder_id> "
                "[--name TEXT] [--description TEXT|--clear-description] [--parent UUID|--clear-parent]"
            ),
            notes=(
                "At least one field is required.",
                "Conflicting nullable pairs are rejected.",
            ),
            example="caption edit_folder <folder-uuid> --description \"Updated\" --clear-parent",
        ),
        CommandSpec(
            name="dl_transcript",
            help="Download captions for a transcript",
            add_arguments=_add_dl_transcript_arguments,
            handler=_handle_dl_transcript,
            default_output="md",
            usage="caption dl_transcript <transcript_id>",
            notes=(
                "Default output is markdown: [HH:MM.SS] speaker: content.",
                "Use --output json for raw payload.",
            ),
            example="caption --output json dl_transcript <transcript-uuid>",
        ),
    )


def run(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    command_specs = {spec.name: spec for spec in _command_specs()}
    selected_command = command_specs.get(args.command)
    if selected_command is None:
        raise CliError(f"Unsupported command: {args.command}")

    config = RuntimeConfig(
        api_url=os.getenv("CAPTION_API_URL"),
        api_token=os.getenv("CAPTION_TOKEN"),
        meili_url=os.getenv("CAPTION_MEILI_URL"),
        cache_path=Path(args.cache_path),
        output=args.output,
    )
    _require_api_url(config)
    if selected_command.needs_meili:
        _require_meili_url(config)

    result = selected_command.handler(config, args)
    emit_output(
        result,
        config.output,
        command_name=args.command,
        search_index=getattr(args, "index", None),
    )
    return 0
