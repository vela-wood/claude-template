import os
import configparser
from pathlib import Path

CONFIG_FILE = Path(os.getenv("CONFIG_PATH", "~/ndcli.cfg")).expanduser()

SEARCH_QUERY = """
SELECT id, name as label
FROM netdocs
WHERE name ILIKE '%' || $1 || '%'
ORDER BY created DESC
LIMIT 10
"""


def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        config.read(CONFIG_FILE)
    return config


def save_config(config: configparser.ConfigParser):
    with open(CONFIG_FILE, "w") as f:
        config.write(f)


def add_recent_matter(config: configparser.ConfigParser, doc_id: str, label: str):
    """Track recently opened matters in config"""
    if "recent_matters" not in config:
        config["recent_matters"] = {}
    config["recent_matters"][doc_id] = label
    save_config(config)


def record_download(config: configparser.ConfigParser, doc_id: str, checksum: str):
    """Record that a file was downloaded with checksum and current date"""
    from datetime import datetime

    if "download_history" not in config:
        config["download_history"] = {}
    date_str = datetime.now().strftime("%Y-%m-%d")
    config["download_history"][doc_id] = f"{checksum}|{date_str}"
    save_config(config)


def get_download_info(config: configparser.ConfigParser, doc_id: str) -> tuple[str, str] | None:
    """Get the checksum and download date for a doc_id, or None if never downloaded"""
    if "download_history" not in config:
        return None
    value = config["download_history"].get(doc_id)
    if value is None:
        return None
    parts = value.split("|", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return None
