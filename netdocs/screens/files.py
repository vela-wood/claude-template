from __future__ import annotations
from typing import TYPE_CHECKING

from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, DataTable, Static, LoadingIndicator

if TYPE_CHECKING:
    from ..app import NDHelper


class FilesScreen(Screen):
    """Screen to list files within a matter/folder"""
    CSS = """
    FilesScreen DataTable {
        margin: 0 2;
        height: 1fr;
    }
    FilesScreen .title {
        margin: 1 2;
        text-style: bold;
    }
    FilesScreen LoadingIndicator {
        height: 3;
    }
    FilesScreen .hidden {
        display: none;
    }
    """
    BINDINGS = [
        ("escape", "go_back", "Back"),
        ("q", "go_back", "Back"),
        ("left", "go_back", "Back"),
    ]

    def __init__(self, doc_id: str, label: str, nd_helper: NDHelper):
        super().__init__()
        self.doc_id = doc_id
        self.label = label
        self.nd_helper = nd_helper
        self._files: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Files in: {self.label}", classes="title")
        yield LoadingIndicator()
        yield DataTable(classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Name", "Type", "Version", "Modified")
        self.load_files()

    @work(exclusive=True, thread=True)
    def load_files(self) -> None:
        try:
            self._files = self.nd_helper.ls(self.doc_id)
            self.app.call_from_thread(self._populate_table)
        except Exception as e:
            self.app.call_from_thread(self._hide_loading)
            self.app.call_from_thread(self.notify, str(e), severity="error")

    def _hide_loading(self) -> None:
        self.query_one(LoadingIndicator).add_class("hidden")
        self.query_one(DataTable).remove_class("hidden")

    def _populate_table(self) -> None:
        self._hide_loading()
        table = self.query_one(DataTable)
        table.clear()
        for f in self._files:
            attrs = f.get("Attributes", {})
            versions = f.get("Versions", {})
            name = attrs.get("Name", "")
            if len(name) > 80:
                name = name[:77] + "..."
            table.add_row(
                name,
                attrs.get("Ext", ""),
                str(versions.get("Official", 1)),
                attrs.get("Modified", "")[:10] if attrs.get("Modified") else "",
            )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()  # Prevent bubbling to App's handler
        self._handle_row_selection()

    def action_select_row(self) -> None:
        self._handle_row_selection()

    def _handle_row_selection(self) -> None:
        table = self.query_one(DataTable)
        row_idx = table.cursor_row
        if row_idx is None or row_idx >= len(self._files):
            return

        f = self._files[row_idx]
        attrs = f.get("Attributes", {})
        versions = f.get("Versions", {})
        doc_id = f.get("DocId", "")
        name = attrs.get("Name", "")
        ext = attrs.get("Ext", "")
        version = versions.get("Official", 1)

        # If it's a folder, navigate into it
        if ext == "ndfld":
            self.app.push_screen(FilesScreen(doc_id, name, self.nd_helper))
            return

        # Download the file
        self._download_file(doc_id, int(version), f"{name}.{ext}")

    @work(thread=True)
    def _download_file(self, doc_id: str, version: int, filename: str) -> None:
        self.app.call_from_thread(self.notify, f"Downloading {filename}...")
        try:
            path = self.nd_helper.download(doc_id, version, filename)
            self.app.call_from_thread(self.notify, f"Downloaded: {path}")
        except Exception as e:
            self.app.call_from_thread(self.notify, str(e), severity="error")

    def action_go_back(self) -> None:
        self.app.pop_screen()
