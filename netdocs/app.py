import os
from pathlib import Path

import asyncpg
import pyperclip
import requests
from dotenv import load_dotenv
from textual import work
from textual.app import App, ComposeResult
from textual.coordinate import Coordinate
from textual.widgets import Header, Footer, Input, DataTable
from textual_fspicker import SelectDirectory

from .config import load_config, save_config, add_recent_matter, SEARCH_QUERY
from .screens import FilesScreen

load_dotenv()


class NDHelper:
    def __init__(self, download_dir: str):
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.api_key = os.getenv("ND_API_KEY")
        self.base_url = os.getenv("NDHELPER_URL")

    def ls(self, docid: str):
        """List all files within a folder represented by a docid"""
        url = f"{self.base_url}/ls/{docid}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = requests.get(url, headers=headers)
        return r.json()["Results"]

    def download(self, docid: str, version: int, fn: str):
        """Download a document by docid and version"""
        url = f"{self.base_url}/dlv/{docid}/{version}"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = requests.get(url, headers=headers)
        full_path = self.download_dir / fn
        with open(full_path, "wb") as f:
            f.write(r.content)
        return str(full_path)


class NetDocsApp(App):
    CSS = """
    Input {
        margin: 1 2;
    }
    DataTable {
        margin: 0 2;
        height: 1fr;
    }
    .recent-label {
        margin: 1 2 0 2;
        color: $text-muted;
    }
    """
    BINDINGS = [
        ("q", "quit", "Quit"),
        ("c", "copy_url", "Copy URL"),
        ("d", "change_download_dir", "Download path"),
    ]

    def __init__(self):
        super().__init__()
        self._pool: asyncpg.Pool | None = None
        self._config = load_config()
        self._nd_helper: NDHelper | None = None

    async def on_mount(self) -> None:
        self._pool = await asyncpg.create_pool(os.environ["MATTERS_DB"])
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("ID", "Label")

        # Check if download_dir is configured
        if "settings" not in self._config or "download_dir" not in self._config["settings"]:
            self._prompt_download_dir()
        else:
            self._nd_helper = NDHelper(self._config["settings"]["download_dir"])
            self._show_recent_matters()

    @work
    async def _prompt_download_dir(self) -> None:
        """Prompt user to select download directory"""
        default_dir = Path()
        selected = await self.push_screen_wait(
            SelectDirectory(default_dir, title="Select Default Download Directory")
        )
        if selected:
            download_dir = str(selected)
        else:
            download_dir = str(default_dir)

        if "settings" not in self._config:
            self._config["settings"] = {}
        self._config["settings"]["download_dir"] = download_dir
        save_config(self._config)
        self._nd_helper = NDHelper(download_dir)
        self.notify(f"Download directory: {download_dir}")
        #self._show_recent_matters()

    def _show_recent_matters(self) -> None:
        """Populate table with recent matters if available"""
        if "recent_matters" in self._config:
            table = self.query_one(DataTable)
            for doc_id, label in self._config["recent_matters"].items():
                table.add_row(doc_id, f"[Recent] {label}")

    async def on_unmount(self) -> None:
        if self._pool:
            await self._pool.close()

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Find a client matter...")
        yield DataTable()
        yield Footer()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.search(event.value)

    def on_key(self, event) -> None:
        if event.key == "down" and self.query_one(Input).has_focus:
            self.query_one(DataTable).focus()
            event.prevent_default()
        elif event.key == "up":
            table = self.query_one(DataTable)
            if table.has_focus and table.cursor_row == 0:
                self.query_one(Input).focus()
                event.prevent_default()

    @work(exclusive=True)
    async def search(self, term: str) -> None:
        table = self.query_one(DataTable)
        table.clear()
        if not term or not self._pool:
            # Show recent matters when search is cleared
            self._show_recent_matters()
            return
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(SEARCH_QUERY, term)
                for row in rows:
                    table.add_row(str(row["id"]), row["label"])
        except Exception as e:
            self.notify(str(e), severity="error")

    def _get_selected_row(self) -> tuple[str, str] | None:
        """Get the currently selected row's doc_id and label."""
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return None
        row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        try:
            row_data = table.get_row(row_key)
            doc_id = row_data[0]
            label = row_data[1].replace("[Recent] ", "")
            return doc_id, label
        except Exception:
            return None

    def on_data_table_row_selected(self, _event: DataTable.RowSelected) -> None:
        selected = self._get_selected_row()
        if not selected:
            return
        doc_id, label = selected
        if self._nd_helper:
            add_recent_matter(self._config, doc_id, label)
            self.push_screen(FilesScreen(doc_id, label, self._nd_helper))
        else:
            self.notify("NDHelper not initialized", severity="error")

    def action_copy_url(self) -> None:
        selected = self._get_selected_row()
        if not selected:
            self.notify("No row selected", severity="warning")
            return
        doc_id, _ = selected
        url = f"https://vault.netvoyage.com/neWeb2/goId.aspx?id={doc_id}"
        pyperclip.copy(url)
        self.notify(f"Copied: {url}")

    def action_change_download_dir(self) -> None:
        self._prompt_download_dir()
