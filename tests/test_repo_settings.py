"""repo_settings.py: JSON object I/O and the strict sidecar_dotfiles read."""

import json
from pathlib import Path

import pytest

import repo_settings
from repo_settings import RepoSettingsError


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(repo_settings, "SETTINGS_PATH", path)
    return path


def test_load_missing_returns_empty(settings_path):
    assert repo_settings.load_json_object() == {}


def test_round_trip(settings_path):
    repo_settings.write_json_object({"a": 1, "sidecar_dotfiles": True})
    assert repo_settings.load_json_object() == {"a": 1, "sidecar_dotfiles": True}
    text = settings_path.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert json.loads(text) == {"a": 1, "sidecar_dotfiles": True}


def test_update_preserves_unrelated_keys(settings_path):
    repo_settings.write_json_object({"keepme": {"x": 1}, "sidecar_dotfiles": False})
    merged = repo_settings.update_json_object({"sidecar_dotfiles": True})
    assert merged == {"keepme": {"x": 1}, "sidecar_dotfiles": True}
    assert repo_settings.load_json_object() == merged


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '"string"', "42"])
def test_malformed_or_non_object_raises(settings_path, content):
    settings_path.write_text(content, encoding="utf-8")
    with pytest.raises(RepoSettingsError):
        repo_settings.load_json_object()


def test_sidecar_dotfiles_missing_is_false(settings_path):
    assert repo_settings.read_sidecar_dotfiles() is False
    repo_settings.write_json_object({"other": 1})
    assert repo_settings.read_sidecar_dotfiles() is False


@pytest.mark.parametrize("value", [True, False])
def test_sidecar_dotfiles_bool(settings_path, value):
    repo_settings.write_json_object({"sidecar_dotfiles": value})
    assert repo_settings.read_sidecar_dotfiles() is value


@pytest.mark.parametrize("value", ["true", 1, 0, None, "yes"])
def test_sidecar_dotfiles_non_bool_raises(settings_path, value):
    settings_path.write_text(
        json.dumps({"sidecar_dotfiles": value}), encoding="utf-8"
    )
    with pytest.raises(RepoSettingsError) as excinfo:
        repo_settings.read_sidecar_dotfiles()
    assert "sidecar_dotfiles" in str(excinfo.value)


def test_explicit_path_wins_over_module_default(settings_path, tmp_path):
    other = tmp_path / "other.json"
    other.write_text('{"sidecar_dotfiles": true}', encoding="utf-8")
    assert repo_settings.read_sidecar_dotfiles(other) is True
    assert repo_settings.read_sidecar_dotfiles() is False
