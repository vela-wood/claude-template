from __future__ import annotations

from pathlib import Path

from dotenv import load_dotenv


def find_repo_root(anchor: str | Path) -> Path:
    current = Path(anchor).resolve()
    if current.is_file():
        current = current.parent

    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").exists():
            return candidate

    return current


def load_repo_dotenv(anchor: str | Path, *, override: bool = True) -> Path:
    repo_root = find_repo_root(anchor)
    env_file = repo_root / ".env"
    return load_dotenv_file(env_file, override=override)


def load_dotenv_file(env_file: str | Path, *, override: bool = True) -> Path:
    env_path = Path(env_file).resolve()
    load_dotenv(dotenv_path=env_path, override=override)
    return env_path
