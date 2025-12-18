from __future__ import annotations
from typing import TYPE_CHECKING

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Header, Footer, DataTable, Static, LoadingIndicator

from ..config import get_download_info, record_download

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
        ("1", "sort_by('name')", "Sort Name"),
        ("2", "sort_by('type')", "Sort Type"),
        ("3", "sort_by('version')", "Sort Version"),
        ("4", "sort_by('modified')", "Sort Modified"),
        ("5", "sort_by('downloaded')", "Sort Downloaded"),
    ]

    def __init__(self, doc_id: str, label: str, nd_helper: NDHelper):
        super().__init__()
        self.doc_id = doc_id
        self.label = label
        self.nd_helper = nd_helper
        self._files: list[dict] = []
        self._sort_column: str | None = None
        self._sort_reverse: bool = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(f"Files in: {self.label}", classes="title")
        yield LoadingIndicator()
        yield DataTable(classes="hidden")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("Name", "Type", "Version", "Modified", "Downloaded")
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
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        config = self.app._config
        for f in self._files:
            attrs = f.get("Attributes", {})
            versions = f.get("Versions", {})
            doc_id = f.get("DocId", "")
            server_checksum = f.get("Checksum", "")
            name = attrs.get("Name", "")
            if len(name) > 80:
                name = name[:77] + "..."
            server_version = versions.get("Official", 1)
            download_info = get_download_info(config, doc_id)

            # Determine if row should be highlighted (checksum mismatch = outdated)
            if download_info is not None:
                local_checksum, download_date = download_info
                is_outdated = local_checksum != server_checksum
            else:
                download_date = ""
                is_outdated = False

            if is_outdated:
                # Highlight row in subtle yellow
                style = "on dark_goldenrod"
                table.add_row(
                    Text(name, style=style),
                    Text(attrs.get("Ext", ""), style=style),
                    Text(str(server_version), style=style),
                    Text(attrs.get("Modified", "")[:10] if attrs.get("Modified") else "", style=style),
                    Text(download_date, style=style),
                )
            else:
                table.add_row(
                    name,
                    attrs.get("Ext", ""),
                    str(server_version),
                    attrs.get("Modified", "")[:10] if attrs.get("Modified") else "",
                    download_date,
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
        checksum = f.get("Checksum", "")
        name = attrs.get("Name", "")
        ext = attrs.get("Ext", "")
        version = versions.get("Official", 1)

        # If it's a folder, navigate into it
        if ext == "ndfld":
            self.app.push_screen(FilesScreen(doc_id, name, self.nd_helper))
            return

        # Download the file
        self._download_file(doc_id, int(version), f"{name}.{ext}", checksum)

    @work(thread=True)
    def _download_file(self, doc_id: str, version: int, filename: str, checksum: str) -> None:
        self.app.call_from_thread(self.notify, f"Downloading {filename}...")
        try:
            path = self.nd_helper.download(doc_id, version, filename)
            # Record the download in config with checksum
            record_download(self.app._config, doc_id, checksum)
            self.app.call_from_thread(self._refresh_table)
            self.app.call_from_thread(self.notify, f"Downloaded: {path}")
        except Exception as e:
            self.app.call_from_thread(self.notify, str(e), severity="error")

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_sort_by(self, column: str) -> None:
        # Toggle direction if same column
        if self._sort_column == column:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_column = column
            self._sort_reverse = False

        # Define sort keys
        config = self.app._config

        def get_sort_key(f: dict):
            attrs = f.get("Attributes", {})
            versions = f.get("Versions", {})
            if column == "name":
                return attrs.get("Name", "").lower()
            elif column == "type":
                return attrs.get("Ext", "").lower()
            elif column == "version":
                return versions.get("Official", 0)
            elif column == "modified":
                return attrs.get("Modified", "") or ""
            elif column == "downloaded":
                info = get_download_info(config, f.get("DocId", ""))
                return info[1] if info is not None else ""
            return ""

        self._files.sort(key=get_sort_key, reverse=self._sort_reverse)
        self._refresh_table()
