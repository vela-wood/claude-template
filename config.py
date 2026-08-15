#!/usr/bin/env python3
"""Launcher for the setup hub (config/app.py): a Textual TUI, human-only —
agents must never run it. Run with `uv run config.py`.
"""
from config.app import main

if __name__ == "__main__":
    raise SystemExit(main())
