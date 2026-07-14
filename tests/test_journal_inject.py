from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
HOOK = REPO_ROOT / ".claude" / "hooks" / "journal_inject.py"


class JournalInjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="journal-inject-test-")
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name) / "journal"
        self.runtime_tmp = Path(self.tempdir.name) / "runtime"
        self.home = Path(self.tempdir.name) / "home"
        self.root.mkdir()
        self.runtime_tmp.mkdir()
        self.home.mkdir()
        self.base_ns = time.time_ns() - 20_000_000_000

    def run_hook(self, prompt: str, session_id: str = "test-session") -> str:
        env = os.environ.copy()
        env["JOURNAL_HOOK_ROOT"] = str(self.root)
        env["TMPDIR"] = str(self.runtime_tmp)
        env["HOME"] = str(self.home)
        env["USERPROFILE"] = str(self.home)
        result = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps({"prompt": prompt, "session_id": session_id}),
            text=True,
            capture_output=True,
            env=env,
            check=False,
            timeout=5,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout

    def write_matter(
        self,
        *,
        code: str = "12345",
        entity: str = "Zephyr Labs LLC",
        stamp: str | None = "20260714",
        open_item: str = "Send signature packet",
    ) -> tuple[Path, Path]:
        folder = self.root / code
        folder.mkdir()
        entry = folder / "20260714_update.md"
        state = folder / "_matter.md"
        stamp_line = f"**State as of:** {stamp}\n\n" if stamp is not None else ""
        entry.write_text("# Dated entry\n", encoding="utf-8")
        state.write_text(
            f"# Matter {code} - Acme / {entity}\n"
            f"{stamp_line}"
            "## Current state\n"
            "- Ready.\n\n"
            "## Open items\n"
            f"- **{open_item}** [owner: counsel; last checked 20260714-1200]\n\n"
            "## Resolved\n"
            "- None.\n",
            encoding="utf-8",
        )
        self.set_mtime(entry, self.base_ns)
        self.set_mtime(state, self.base_ns + 5_000_000_000)
        return state, entry

    @staticmethod
    def set_mtime(path: Path, value_ns: int) -> None:
        os.utime(path, ns=(value_ns, value_ns))

    def test_no_match_is_silent(self) -> None:
        self.write_matter()

        output = self.run_hook("Prepare an unrelated generic document")

        self.assertEqual(output, "")

    def test_numeric_code_match_injects_current_state_and_bullet_digest(self) -> None:
        self.write_matter()

        output = self.run_hook("Review matter 12345 before closing")

        self.assertIn('<matter-journal matter="12345" state="CURRENT"', output)
        self.assertIn("Open items (1):", output)
        self.assertIn("Send signature packet", output)
        self.assertIn("--- FULL STATE FILE BELOW ---", output)
        self.assertIn("## Current state", output)

    def test_multi_token_entity_name_match_injects(self) -> None:
        self.write_matter(entity="Zephyr Labs LLC")

        output = self.run_hook("Continue the Zephyr Labs closing", session_id="name-session")

        self.assertIn('<matter-journal matter="12345" state="CURRENT"', output)

    def test_same_session_suppresses_unchanged_reinjection(self) -> None:
        self.write_matter()
        first = self.run_hook("Review matter 12345", session_id="repeat-session")

        second = self.run_hook("Review matter 12345 again", session_id="repeat-session")

        self.assertIn("<matter-journal ", first)
        self.assertEqual(second, "")

    def test_same_session_emits_short_notice_after_state_changes(self) -> None:
        state, _ = self.write_matter()
        self.run_hook("Review matter 12345", session_id="changed-session")
        state.write_text(
            state.read_text(encoding="utf-8") + "\nParallel-session update.\n",
            encoding="utf-8",
        )
        self.set_mtime(state, self.base_ns + 10_000_000_000)

        output = self.run_hook("Review matter 12345 again", session_id="changed-session")

        self.assertIn('<matter-journal-update matter="12345" state="CURRENT"', output)
        self.assertIn("CHANGED since it was injected", output)
        self.assertNotIn("--- FULL STATE FILE BELOW ---", output)

    def test_entry_less_than_two_seconds_newer_marks_state_stale(self) -> None:
        state, entry = self.write_matter()
        self.set_mtime(state, self.base_ns)
        self.set_mtime(entry, self.base_ns + 500_000_000)
        if entry.stat().st_mtime_ns <= state.stat().st_mtime_ns:
            self.skipTest("filesystem does not preserve sub-two-second mtime ordering")

        output = self.run_hook("Review matter 12345", session_id="mtime-session")

        self.assertIn('<matter-journal matter="12345" state="STALE"', output)
        self.assertIn("20260714_update.md", output)
        self.assertIn("NEWER than the state file", output)

    def test_stamp_older_than_entry_filename_marks_state_stale(self) -> None:
        self.write_matter(stamp="20260713")

        output = self.run_hook("Review matter 12345", session_id="stamp-session")

        self.assertIn('<matter-journal matter="12345" state="STALE"', output)
        self.assertIn("State-as-of stamp (20260713)", output)
        self.assertIn("newest dated entry (20260714)", output)

    def test_missing_stamp_marks_state_unstamped(self) -> None:
        self.write_matter(stamp=None)

        output = self.run_hook("Review matter 12345", session_id="unstamped-session")

        self.assertIn('<matter-journal matter="12345" state="UNSTAMPED"', output)
        self.assertIn("no State-as-of stamp", output)

    def test_generic_single_entity_token_does_not_match(self) -> None:
        self.write_matter(entity="Vision LLC")

        output = self.run_hook("Please revise our vision statement")

        self.assertEqual(output, "")

    def test_flat_blog_bucket_injects(self) -> None:
        entry = self.root / "VW-Internal-Blogs_20260714_update.md"
        state = self.root / "VW-Internal-Blogs__matter.md"
        entry.write_text("# Blog dated entry\n", encoding="utf-8")
        state.write_text(
            "# VW Internal Blogs\n"
            "**State as of:** 20260714\n\n"
            "## Current state\n"
            "- Editorial calendar ready.\n\n"
            "## Open items\n"
            "- **Draft launch article** [owner: marketing; last checked 20260714-1200]\n",
            encoding="utf-8",
        )
        self.set_mtime(entry, self.base_ns)
        self.set_mtime(state, self.base_ns + 5_000_000_000)

        output = self.run_hook("Draft the next blog post", session_id="flat-session")

        self.assertIn(
            '<matter-journal matter="VW-Internal-Blogs" state="CURRENT"',
            output,
        )
        self.assertIn("Draft launch article", output)


if __name__ == "__main__":
    unittest.main()
