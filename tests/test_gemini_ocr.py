"""Tests for gemini_ocr.py: selection, fan-out, assembly, indexes, exit codes.

No network: the CLI's client factory is monkeypatched to a FakePageOcr, and
the SDK layer is tested against a SimpleNamespace shaped like a response.
"""

import asyncio
import csv
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_digital_pdf, make_scanned_pdf

import gemini_ocr
import startup
import startup_lib.common
import startup_lib.gemini_ocr as driver
from startup_lib.gemini_client import (
    IMAGE_MIME,
    GeminiPageOcr,
    PageFailed,
    PageText,
    ThinkingLevel,
)
from startup_lib.gemini_client import _strip_fence


def read_csv_dict(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


class FakePageOcr:
    """Canned Markdown per call; records concurrency; fails page k on demand."""

    def __init__(self, fail_call: int | None = None, error: Exception | None = None):
        self.calls = 0
        self.in_flight = 0
        self.max_in_flight = 0
        self.fail_call = fail_call
        self.error = error or PageFailed("boom")

    async def ocr_page(self, image: bytes) -> PageText:
        assert image[:2] == b"\xff\xd8", "expected JPEG bytes"
        self.calls += 1
        call = self.calls
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            if call == self.fail_call:
                raise self.error
            return PageText(f"Text of call {call}", 1000, 500)
        finally:
            self.in_flight -= 1


@pytest.fixture
def gemini_env(repo_tmp, monkeypatch):
    monkeypatch.setattr(gemini_ocr, "_load_env", lambda: None)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    state = {"fake": FakePageOcr(), "factory_calls": 0}

    def factory(key, level):
        state["factory_calls"] += 1
        return state["fake"]

    monkeypatch.setattr(gemini_ocr, "_make_client", factory)
    return repo_tmp, state


def ocr_row(root: Path, rel: str) -> dict:
    rows = {r["file"]: r for r in read_csv_dict(root / ".ocr_index.csv")}
    return rows[rel]


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_two_page_scan_writes_sidecar_and_indexes(gemini_env, capsys):
    root, state = gemini_env
    make_scanned_pdf(root / "scan.pdf", pages=2)

    assert gemini_ocr.main(["scan.pdf"]) == 0

    sidecar = root / "scan.pdf.md"
    text = sidecar.read_text(encoding="utf-8")
    assert text.startswith("<!-- OCR by gemini-3.8-flash on ")
    assert text.index("<!-- page 1 -->") < text.index("<!-- page 2 -->")
    assert "Text of call" in text

    row = ocr_row(root, "scan.pdf")
    assert row["verdict"] == "scanned-image-only"
    assert row["ocr_done"] == "true"
    assert {r["file"] for r in read_csv_dict(root / ".hash_index.csv")} == {"scan.pdf"}
    assert {r["file"] for r in read_csv_dict(root / ".token_index.csv")} == {"scan.pdf.md"}

    # startup.py must treat the result as certified and leave the bytes alone.
    before = sidecar.read_bytes()
    capsys.readouterr()
    assert startup.main() == 0
    out = capsys.readouterr().out
    assert "1 unchanged" in out
    assert "may need OCR: 0" in out
    assert sidecar.read_bytes() == before


def test_dotfile_sidecar_style(gemini_env):
    root, _ = gemini_env
    (root / "settings.json").write_text(json.dumps({"sidecar_dotfiles": True}))
    make_scanned_pdf(root / "scan.pdf", pages=1)

    assert gemini_ocr.main(["scan.pdf"]) == 0
    assert (root / ".scan.pdf.md").exists()
    assert not (root / "scan.pdf.md").exists()


def test_rerun_skips_then_force_reocrs(gemini_env, capsys):
    root, state = gemini_env
    make_scanned_pdf(root / "scan.pdf", pages=2)
    assert gemini_ocr.main(["scan.pdf"]) == 0
    assert state["fake"].calls == 2

    capsys.readouterr()
    assert gemini_ocr.main(["scan.pdf"]) == 0
    assert "skip scan.pdf: OCR output already current" in capsys.readouterr().out
    assert state["fake"].calls == 2

    assert gemini_ocr.main(["scan.pdf", "--force"]) == 0
    assert state["fake"].calls == 4


def test_page_failure_fails_file_only(gemini_env):
    root, state = gemini_env
    make_scanned_pdf(root / "a_bad.pdf", pages=4)
    make_scanned_pdf(root / "b_good.pdf", pages=2)
    state["fake"] = FakePageOcr(fail_call=2)

    assert gemini_ocr.main(["a_bad.pdf", "b_good.pdf", "--concurrency", "1"]) == 1

    assert not (root / "a_bad.pdf.md").exists()
    assert (root / "b_good.pdf.md").exists()
    assert ocr_row(root, "a_bad.pdf")["ocr_done"] == ""
    assert ocr_row(root, "b_good.pdf")["ocr_done"] == "true"
    assert {r["file"] for r in read_csv_dict(root / ".hash_index.csv")} == {"b_good.pdf"}
    # Pages 3-4 of a_bad were never requested: 2 (a_bad) + 2 (b_good).
    assert state["fake"].calls == 4


def test_concurrency_bounds_in_flight(gemini_env):
    root, state = gemini_env
    make_scanned_pdf(root / "scan.pdf", pages=6)

    assert gemini_ocr.main(["scan.pdf", "--concurrency", "2"]) == 0
    assert state["fake"].max_in_flight == 2
    assert state["fake"].calls == 6


def test_missing_key_exits_2(gemini_env, monkeypatch, capsys):
    root, state = gemini_env
    monkeypatch.delenv("GEMINI_API_KEY")
    make_scanned_pdf(root / "scan.pdf", pages=1)

    assert gemini_ocr.main(["scan.pdf"]) == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err
    assert state["factory_calls"] == 0

    assert gemini_ocr.main(["scan.pdf", "--dry-run"]) == 0
    assert state["factory_calls"] == 0
    assert not (root / "scan.pdf.md").exists()
    assert not (root / ".hash_index.csv").exists()


def test_directory_scope_and_explicit_digital(gemini_env):
    root, state = gemini_env
    (root / "docs").mkdir()
    make_scanned_pdf(root / "docs" / "scan.pdf", pages=1)
    make_digital_pdf(root / "docs" / "digital.pdf", ["A born-digital page with plenty of text."])

    assert gemini_ocr.main(["docs"]) == 0
    assert (root / "docs" / "scan.pdf.md").exists()
    assert not (root / "docs" / "digital.pdf.md").exists()

    assert gemini_ocr.main(["docs", "--all"]) == 0
    assert (root / "docs" / "digital.pdf.md").exists()
    assert ocr_row(root, "docs/digital.pdf")["ocr_done"] == "true"

    (root / "docs" / "digital.pdf.md").unlink()
    assert gemini_ocr.main(["docs/digital.pdf"]) == 0
    assert (root / "docs" / "digital.pdf.md").exists()


def test_path_outside_cwd_exits_1(gemini_env, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    make_scanned_pdf(outside / "scan.pdf", pages=1)
    assert gemini_ocr.main([str(outside / "scan.pdf")]) == 1


def test_fatal_failure_aborts_run(gemini_env):
    root, state = gemini_env
    make_scanned_pdf(root / "a.pdf", pages=5)
    make_scanned_pdf(root / "b.pdf", pages=5)
    state["fake"] = FakePageOcr(fail_call=1, error=PageFailed("401 bad key", fatal=True))

    assert gemini_ocr.main(["a.pdf", "b.pdf", "--concurrency", "1"]) == 1
    assert not (root / "a.pdf.md").exists()
    assert not (root / "b.pdf.md").exists()
    assert state["fake"].calls < 10


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------


def test_assemble_markdown(monkeypatch):
    today = date(2026, 9, 5)
    text = driver.assemble_markdown(["One", "Two"], "m", today)
    assert text == (
        "<!-- OCR by m on 2026-09-05; 2 page(s); media_resolution high -->\n\n"
        "<!-- page 1 -->\n\nOne\n\n<!-- page 2 -->\n\nTwo\n"
    )

    monkeypatch.setattr(driver, "PAGE_MARKERS", False)
    text = driver.assemble_markdown(["One", "Two"], "m", today)
    assert text.endswith("-->\n\nOne\n\nTwo\n")


def test_strip_fence():
    assert _strip_fence("```markdown\n# H\n```") == "# H"
    assert _strip_fence("# H") == "# H"


def _response(text, reason="STOP", prompt=1120, out=300, thoughts=50):
    return SimpleNamespace(
        text=text,
        candidates=[SimpleNamespace(finish_reason=SimpleNamespace(name=reason))],
        usage_metadata=SimpleNamespace(
            prompt_token_count=prompt,
            candidates_token_count=out,
            thoughts_token_count=thoughts,
        ),
    )


class FakeSdk:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.aio = SimpleNamespace(models=SimpleNamespace(generate_content=self._gen))

    async def _gen(self, **kwargs):
        self.requests.append(kwargs)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_gemini_client_request_shape(monkeypatch):
    from google.genai import types

    sdk = FakeSdk([_response("# Page")])
    monkeypatch.setattr("startup_lib.gemini_client.make_client", lambda key: sdk)
    ocr = GeminiPageOcr("k", ThinkingLevel.LOW)

    page = asyncio.run(ocr.ocr_page(b"\xff\xd8jpeg"))
    assert page == PageText("# Page", 1120, 350)

    request = sdk.requests[0]
    assert request["model"] == "gemini-3.8-flash"
    config = request["config"]
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config.temperature is None
    part = request["contents"][1]
    assert part.inline_data.mime_type == IMAGE_MIME
    assert part.media_resolution.level == types.PartMediaResolutionLevel.MEDIA_RESOLUTION_HIGH


def test_gemini_client_max_tokens_twice_fails(monkeypatch):
    sdk = FakeSdk([_response("x", reason="MAX_TOKENS"), _response("x", reason="MAX_TOKENS")])
    monkeypatch.setattr("startup_lib.gemini_client.make_client", lambda key: sdk)
    ocr = GeminiPageOcr("k", ThinkingLevel.LOW)

    with pytest.raises(PageFailed) as info:
        asyncio.run(ocr.ocr_page(b"\xff\xd8"))
    assert "MAX_TOKENS" in info.value.detail
    assert not info.value.fatal
    assert len(sdk.requests) == 2


def test_gemini_client_401_is_fatal(monkeypatch):
    from google.genai import errors

    err = errors.APIError(401, {"error": {"message": "bad key", "status": "UNAUTHENTICATED"}})
    sdk = FakeSdk([err])
    monkeypatch.setattr("startup_lib.gemini_client.make_client", lambda key: sdk)
    ocr = GeminiPageOcr("k", ThinkingLevel.LOW)

    with pytest.raises(PageFailed) as info:
        asyncio.run(ocr.ocr_page(b"\xff\xd8"))
    assert info.value.fatal
    assert "401" in info.value.detail
