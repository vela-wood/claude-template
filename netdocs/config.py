import os
import configparser
from pathlib import Path

CONFIG_FILE = Path(os.getenv("CONFIG_PATH", "netdocs.cfg"))

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
