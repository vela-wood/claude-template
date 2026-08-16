"""Orchestration tests for startup.py: discovery, hashing, concurrency,
status aggregation, staging, persistence, cleanup, and exit codes.

Failures are injected deterministically via monkeypatching rather than
relying on operating-system permission behavior.
"""

import csv
import io
import json
import threading
import time
from pathlib import Path

import pytest
from conftest import (
    SIMPLE_EML,
    make_digital_pdf,
    make_malformed_pdf,
    make_scanned_pdf,
)

import startup


def write_eml(path: Path, subject: str = "s") -> Path:
    path.write_bytes(SIMPLE_EML.replace(b"Simple message", subject.encode()))
    return path


def run_main() -> int:
    return startup.main()


def read_csv_dict(path: Path) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discovery_prunes_hidden_dirs_and_caption_cache(repo_tmp):
    write_eml(repo_tmp / "top.eml")
    (repo_tmp / ".hiddendir").mkdir()
    write_eml(repo_tmp / ".hiddendir" / "skipme.eml")
    (repo_tmp / "caption_cache").mkdir()
    write_eml(repo_tmp / "caption_cache" / "skipme2.eml")
    (repo_tmp / "sub").mkdir()
    write_eml(repo_tmp / "sub" / "nested.eml")
    write_eml(repo_tmp / ".dotfile.eml")  # dot-prefixed FILE stays eligible
    write_eml(repo_tmp / "~temp.eml")  # tilde temp file skipped
    (repo_tmp / "notice.mbx").write_bytes(b"whatever")

    sources, mbx = startup.discover_sources(repo_tmp)
    rels = {str(p.relative_to(repo_tmp)) for p in sources}
    assert rels == {"top.eml", "sub/nested.eml", ".dotfile.eml"}
    assert [str(p.relative_to(repo_tmp)) for p in mbx] == ["notice.mbx"]


def test_mbx_noticed_skipped_and_absent_from_indexes(repo_tmp, capsys):
    write_eml(repo_tmp / "real.eml")
    (repo_tmp / "legacy.mbx").write_bytes(b"binary eudora stuff")

    assert run_main() == 0
    out = capsys.readouterr().out
    assert "legacy.mbx" in out
    assert ".mbx" in out and "not converted" in out
    assert not (repo_tmp / "legacy.mbx.md").exists()
    hash_rows = {r["file"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")}
    token_rows = {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")}
    assert hash_rows == {"real.eml"}
    assert token_rows == {"real.eml.md"}


def test_stale_mbx_index_rows_pruned_but_sidecar_untouched(repo_tmp):
    write_eml(repo_tmp / "real.eml")
    (repo_tmp / "legacy.mbx").write_bytes(b"x")
    old_sidecar = repo_tmp / "legacy.mbx.md"
    old_sidecar.write_text("previously converted mbx", encoding="utf-8")
    (repo_tmp / ".hash_index.csv").write_text(
        "file,hash\nlegacy.mbx,deadbeef\n", encoding="utf-8"
    )
    (repo_tmp / ".token_index.csv").write_text(
        "file,tokens\nlegacy.mbx.md,10\n", encoding="utf-8"
    )

    assert run_main() == 0
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")} == {"real.eml"}
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {"real.eml.md"}
    assert old_sidecar.read_text(encoding="utf-8") == "previously converted mbx"


# ---------------------------------------------------------------------------
# Idempotence and certification
# ---------------------------------------------------------------------------


def test_second_run_performs_zero_conversions(repo_tmp, monkeypatch, capsys):
    write_eml(repo_tmp / "a.eml")
    write_eml(repo_tmp / "b.eml")
    assert run_main() == 0
    assert "2 converted" in capsys.readouterr().out

    calls = []
    original = startup.convert_to_markdown
    monkeypatch.setattr(
        startup, "convert_to_markdown", lambda s: calls.append(s) or original(s)
    )
    assert run_main() == 0
    out = capsys.readouterr().out
    assert calls == []
    assert "0 converted, 2 unchanged, 0 failed, 0 deferred for OCR" in out


def test_missing_token_row_is_retokenized_not_reconverted(
    repo_tmp, monkeypatch, capsys
):
    """Sidecar + matching certified hash but no token row → the migration
    repair re-tokenizes the existing sidecar instead of reconverting."""
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    sidecar_bytes = (repo_tmp / "a.eml.md").read_bytes()
    (repo_tmp / ".token_index.csv").write_text("file,tokens\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: calls.append(s) or "x")
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "0 converted, 1 unchanged" in out
    assert "1 re-tokenized" in out
    assert calls == []
    assert (repo_tmp / "a.eml.md").read_bytes() == sidecar_bytes
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {"a.eml.md"}


def test_invalid_token_row_is_retokenized_not_reconverted(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    (repo_tmp / ".token_index.csv").write_text(
        "file,tokens\na.eml.md,notanumber\n", encoding="utf-8"
    )

    calls = []
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: calls.append(s) or "x")
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "0 converted, 1 unchanged" in out
    assert calls == []
    rows = read_csv_dict(repo_tmp / ".token_index.csv")
    assert [r["file"] for r in rows] == ["a.eml.md"]
    assert rows[0]["tokens"].isdigit()


def test_hash_row_behind_sidecar_causes_reconversion(repo_tmp, capsys):
    """Interrupted write: sidecar/token rows ahead of the hash index."""
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    (repo_tmp / ".hash_index.csv").write_text("file,hash\n", encoding="utf-8")
    assert run_main() == 0
    assert "1 converted, 0 unchanged" in capsys.readouterr().out


def test_changed_content_reconverts_even_with_same_size(repo_tmp, capsys):
    write_eml(repo_tmp / "a.eml", subject="AAAAAAA")
    assert run_main() == 0
    capsys.readouterr()
    write_eml(repo_tmp / "a.eml", subject="BBBBBBB")  # same byte length
    assert run_main() == 0
    assert "1 converted, 0 unchanged" in capsys.readouterr().out
    assert "# BBBBBBB" in (repo_tmp / "a.eml.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Hash failures
# ---------------------------------------------------------------------------


def _raise_for(target: Path, original):
    def fake(path: Path):
        if path == target:
            raise OSError("injected read failure")
        return original(path)

    return fake


def test_new_source_hash_failure(repo_tmp, monkeypatch, capsys):
    write_eml(repo_tmp / "good.eml")
    bad = write_eml(repo_tmp / "bad.eml")
    monkeypatch.setattr(startup, "hash_file", _raise_for(bad, startup.hash_file))

    assert run_main() == 1
    out = capsys.readouterr().out
    assert "1 converted" in out and "1 failed" in out
    assert not (repo_tmp / "bad.eml.md").exists()
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")} == {"good.eml"}
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {"good.eml.md"}


def test_indexed_source_hash_failure_preserves_prior_state(
    repo_tmp, monkeypatch, capsys
):
    bad = write_eml(repo_tmp / "bad.eml")
    make_digital_pdf(repo_tmp / "doc.pdf")
    assert run_main() == 0
    capsys.readouterr()
    prior_hash = read_csv_dict(repo_tmp / ".hash_index.csv")
    prior_token = read_csv_dict(repo_tmp / ".token_index.csv")
    prior_ocr = read_csv_dict(repo_tmp / ".ocr_index.csv")
    prior_sidecar = (repo_tmp / "bad.eml.md").read_bytes()

    calls = []
    original_convert = startup.convert_to_markdown
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda s: calls.append(s) or original_convert(s),
    )
    monkeypatch.setattr(startup, "hash_file", _raise_for(bad, startup.hash_file))

    assert run_main() == 1
    assert bad not in calls  # not converted
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == prior_hash
    assert read_csv_dict(repo_tmp / ".token_index.csv") == prior_token
    assert read_csv_dict(repo_tmp / ".ocr_index.csv") == prior_ocr
    assert (repo_tmp / "bad.eml.md").read_bytes() == prior_sidecar


# ---------------------------------------------------------------------------
# Conversion failures
# ---------------------------------------------------------------------------


def test_failed_conversion_keeps_stale_sidecar_and_old_hash(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml", subject="OLDSUBJ")
    assert run_main() == 0
    capsys.readouterr()
    old_rows = {r["file"]: r["hash"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")}
    old_sidecar = (repo_tmp / "a.eml.md").read_bytes()

    write_eml(repo_tmp / "a.eml", subject="NEWSUBJ")

    original = startup.convert_to_markdown

    def boom(src):
        raise RuntimeError("injected converter failure")

    monkeypatch.setattr(startup, "convert_to_markdown", boom)
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "0 converted, 0 unchanged, 1 failed" in out
    assert "FAILED a.eml" in out
    # stale sidecar retained; hash NOT advanced to the new content
    assert (repo_tmp / "a.eml.md").read_bytes() == old_sidecar
    new_rows = {r["file"]: r["hash"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")}
    assert new_rows == old_rows

    # next run with a working converter retries and certifies
    monkeypatch.setattr(startup, "convert_to_markdown", original)
    assert run_main() == 0
    assert "# NEWSUBJ" in (repo_tmp / "a.eml.md").read_text(encoding="utf-8")


def test_token_count_failure_preserves_prior_certification(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml", subject="OLDSUBJ")
    assert run_main() == 0
    capsys.readouterr()
    old_sidecar = (repo_tmp / "a.eml.md").read_bytes()
    old_hash = read_csv_dict(repo_tmp / ".hash_index.csv")

    write_eml(repo_tmp / "a.eml", subject="NEWSUBJ")

    def boom(path):
        raise RuntimeError("injected tokenizer failure")

    monkeypatch.setattr(startup, "count_tokens", boom)
    assert run_main() == 1
    assert (repo_tmp / "a.eml.md").read_bytes() == old_sidecar
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == old_hash


def test_classification_failure_excludes_pdf_and_exits_nonzero(
    repo_tmp, monkeypatch, capsys
):
    make_malformed_pdf(repo_tmp / "broken.pdf")
    write_eml(repo_tmp / "ok.eml")

    calls = []
    original = startup.convert_to_markdown
    monkeypatch.setattr(
        startup, "convert_to_markdown", lambda s: calls.append(s) or original(s)
    )
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "1 converted" in out and "1 failed" in out
    assert all(p.suffix != ".pdf" for p in calls)  # never reached the converter
    assert read_csv_dict(repo_tmp / ".ocr_index.csv") == []  # no row staged


def test_source_mutation_during_conversion_fails(repo_tmp, monkeypatch, capsys):
    src = write_eml(repo_tmp / "a.eml")

    original = startup.convert_to_markdown

    def mutate_then_convert(path):
        text = original(path)
        write_eml(src, subject="MUTATED-DURING-RUN")
        return text

    monkeypatch.setattr(startup, "convert_to_markdown", mutate_then_convert)
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "source changed during conversion" in out
    assert not (repo_tmp / "a.eml.md").exists()
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []


def test_empty_converter_output_is_failure(repo_tmp, monkeypatch, capsys):
    write_eml(repo_tmp / "a.eml")
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: "")
    assert run_main() == 1
    assert not (repo_tmp / "a.eml.md").exists()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_converter_concurrency_capped_at_four(repo_tmp, monkeypatch):
    for i in range(9):
        write_eml(repo_tmp / f"m{i}.eml", subject=f"subj {i}")

    lock = threading.Lock()
    active = 0
    max_active = 0
    converted = []
    original = startup.convert_to_markdown

    def tracking_convert(src):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            return original(src)
        finally:
            with lock:
                active -= 1
            converted.append(src.name)

    monkeypatch.setattr(startup, "convert_to_markdown", tracking_convert)
    assert run_main() == 0
    assert len(converted) == 9  # every result collected
    assert max_active <= 4


# ---------------------------------------------------------------------------
# OCR state machine
# ---------------------------------------------------------------------------


class _FakeFocrProc:
    """subprocess.Popen stand-in for run_ocr's streaming read loop."""

    def __init__(self, stdout: str, returncode: int = 0):
        self.stdout = io.StringIO(stdout)
        self.returncode = returncode
        self.terminated = False
        self.waited = False

    def wait(self):
        self.waited = True
        return self.returncode

    def terminate(self):
        self.terminated = True


def _focr_images(cmd) -> list[str]:
    return [arg for arg in cmd if str(arg).endswith(".png")]


def _focr_wrapper_json(images, **kwargs) -> str:
    """The wrapper object focr 0.7.2 emits, all at once, at exit."""
    return json.dumps(
        {
            "results": [
                {"image": img, "ok": True, "markdown": f"OCR text for {Path(img).name}"}
                for img in images
            ]
        },
        **kwargs,
    )


def _fake_focr_success(cmd, **kwargs):
    return _FakeFocrProc(_focr_wrapper_json(_focr_images(cmd)))


def test_pending_ocr_without_flag_is_deferred_exit_zero(repo_tmp, monkeypatch, capsys):
    make_scanned_pdf(repo_tmp / "scan.pdf")
    calls = []
    monkeypatch.setattr(
        startup, "convert_to_markdown", lambda s: calls.append(s) or "x"
    )
    assert run_main() == 0  # pending consent alone exits zero
    out = capsys.readouterr().out
    assert "0 converted, 0 unchanged, 0 failed, 1 deferred for OCR" in out
    assert "Ask the user before running" in out
    assert calls == []  # never fell through to the generic converter
    assert not (repo_tmp / "scan.pdf.md").exists()
    ocr_rows = read_csv_dict(repo_tmp / ".ocr_index.csv")
    assert len(ocr_rows) == 1 and ocr_rows[0]["ocr_done"] == ""
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []  # not certified


def test_unresolved_scanned_pdf_conflict_is_excluded_from_every_route(
    repo_tmp, monkeypatch, capsys
):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf")
    preferred = repo_tmp / "scan.pdf.md"
    alternate = repo_tmp / ".scan.pdf.md"
    preferred.write_bytes(b"first indexed OCR artifact")
    alternate.write_bytes(b"second indexed OCR artifact")
    write_token_map(repo_tmp, {"scan.pdf.md": 1, ".scan.pdf.md": 2})
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])

    def should_not_run(*args, **kwargs):
        raise AssertionError("unresolved PDF conflict reached processing")

    monkeypatch.setattr(startup, "classify_pdf", should_not_run)
    monkeypatch.setattr(startup, "run_ocr", should_not_run)
    monkeypatch.setattr(startup, "convert_to_markdown", should_not_run)
    monkeypatch.setattr(startup, "count_tokens", should_not_run)

    assert run_main() == 1
    out = capsys.readouterr().out
    assert "scan.pdf.md and .scan.pdf.md" in out
    assert preferred.read_bytes() == b"first indexed OCR artifact"
    assert alternate.read_bytes() == b"second indexed OCR artifact"
    assert token_map(repo_tmp) == {"scan.pdf.md": 1, ".scan.pdf.md": 2}
    assert read_csv_dict(repo_tmp / ".ocr_index.csv") == []
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []


def test_requested_ocr_success_certifies_exactly_once(repo_tmp, monkeypatch, capsys):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf", pages=2)
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])
    calls = []
    monkeypatch.setattr(
        startup, "convert_to_markdown", lambda s: calls.append(s) or "x"
    )
    monkeypatch.setattr(startup.subprocess, "Popen", _fake_focr_success)

    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 converted, 0 unchanged, 0 failed, 0 deferred for OCR" in out
    assert calls == []  # OCR-routed source never invokes the generic converter
    sidecar = (repo_tmp / "scan.pdf.md").read_text(encoding="utf-8")
    assert sidecar.count("OCR text for") == 2  # both pages, joined
    ocr_rows = read_csv_dict(repo_tmp / ".ocr_index.csv")
    assert ocr_rows[0]["ocr_done"] == "true"
    token_rows = read_csv_dict(repo_tmp / ".token_index.csv")
    assert [r["file"] for r in token_rows] == ["scan.pdf.md"]  # tokenized once
    assert [r["file"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")] == ["scan.pdf"]

    # A following run is fully unchanged: OCR output not overwritten
    monkeypatch.setattr(sys, "argv", ["startup.py"])
    assert run_main() == 0
    assert "0 converted, 1 unchanged, 0 failed, 0 deferred for OCR" in capsys.readouterr().out
    assert (repo_tmp / "scan.pdf.md").read_text(encoding="utf-8") == sidecar


def test_requested_ocr_failure_is_nonzero_and_never_falls_through(
    repo_tmp, monkeypatch, capsys
):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf")
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])
    calls = []
    monkeypatch.setattr(
        startup, "convert_to_markdown", lambda s: calls.append(s) or "x"
    )

    monkeypatch.setattr(
        startup.subprocess, "Popen", lambda cmd, **kw: _FakeFocrProc("", returncode=3)
    )
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "0 converted, 0 unchanged, 1 failed" in out
    assert calls == []
    assert not (repo_tmp / "scan.pdf.md").exists()
    assert read_csv_dict(repo_tmp / ".ocr_index.csv")[0]["ocr_done"] == ""
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []


def test_authorized_ocr_failure_keeps_preserved_unindexed_backup(
    repo_tmp, monkeypatch
):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf")
    unindexed = repo_tmp / "scan.pdf.md"
    unindexed.write_bytes(b"preserve before failed OCR")
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])

    monkeypatch.setattr(
        startup.subprocess, "Popen", lambda cmd, **kw: _FakeFocrProc("", returncode=7)
    )
    assert run_main() == 1
    backups = preserved_backups(repo_tmp, "scan.pdf.md")
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"preserve before failed OCR"
    assert not unindexed.exists()
    assert read_csv_dict(repo_tmp / ".token_index.csv") == []
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []
    ocr_rows = read_csv_dict(repo_tmp / ".ocr_index.csv")
    assert len(ocr_rows) == 1 and ocr_rows[0]["ocr_done"] == ""


# ---------------------------------------------------------------------------
# OCR streaming, chunking, and partial success
# ---------------------------------------------------------------------------


def _ocr_two_scans(repo_tmp, monkeypatch):
    """Two 2-page scanned PDFs queued for an authorized OCR run."""
    import sys

    make_scanned_pdf(repo_tmp / "one.pdf", pages=2)
    make_scanned_pdf(repo_tmp / "two.pdf", pages=2)
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])


def test_pretty_printed_focr_json_uses_whole_payload_fallback(
    repo_tmp, monkeypatch, capsys
):
    """focr 0.7.2 buffers its payload and may indent it across lines: no
    single line parses, so the whole-stdout parser must still finish."""
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf", pages=2)
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])
    monkeypatch.setattr(
        startup.subprocess,
        "Popen",
        lambda cmd, **kw: _FakeFocrProc(_focr_wrapper_json(_focr_images(cmd), indent=2)),
    )

    assert run_main() == 0
    assert "1 converted, 0 unchanged, 0 failed" in capsys.readouterr().out
    assert (repo_tmp / "scan.pdf.md").read_text(encoding="utf-8").count(
        "OCR text for"
    ) == 2


def test_nonzero_exit_keeps_completed_pdfs_and_fails_only_the_rest(
    repo_tmp, monkeypatch, capsys
):
    """A crash mid-batch must not throw away the PDFs already finished."""
    _ocr_two_scans(repo_tmp, monkeypatch)

    def partial_then_die(cmd, **kw):
        images = _focr_images(cmd)
        done = [img for img in images if "d0000-" in img]  # first PDF only
        return _FakeFocrProc(_focr_wrapper_json(done), returncode=9)

    monkeypatch.setattr(startup.subprocess, "Popen", partial_then_die)

    assert run_main() == 1
    out = capsys.readouterr().out
    assert "1 converted, 0 unchanged, 1 failed" in out
    assert "focr ocr-batch exited 9" in out
    assert (repo_tmp / "one.pdf.md").exists()
    assert not (repo_tmp / "two.pdf.md").exists()
    # the finished PDF is certified; the failed one keeps no OCR state
    assert [r["file"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")] == ["one.pdf"]
    done_rows = {r["file"]: r["ocr_done"] for r in read_csv_dict(repo_tmp / ".ocr_index.csv")}
    assert done_rows == {"one.pdf": "true", "two.pdf": ""}


def test_interrupt_keeps_completed_pdfs_and_fails_the_rest(
    repo_tmp, monkeypatch, capsys
):
    """Ctrl+C: finished sidecars persist, the rest fail, exit is nonzero."""
    _ocr_two_scans(repo_tmp, monkeypatch)

    class InterruptingProc(_FakeFocrProc):
        def __init__(self, cmd, **kw):
            done = [img for img in _focr_images(cmd) if "d0000-" in img]
            super().__init__(_focr_wrapper_json(done))
            self._lines = list(self.stdout)
            self.stdout = self

        def __iter__(self):
            yield from self._lines
            raise KeyboardInterrupt

        def close(self):
            pass

    monkeypatch.setattr(startup.subprocess, "Popen", InterruptingProc)

    assert run_main() == 1
    out = capsys.readouterr().out
    assert "1 converted, 0 unchanged, 1 failed" in out
    assert "interrupted before OCR finished" in out
    assert (repo_tmp / "one.pdf.md").exists()
    assert not (repo_tmp / "two.pdf.md").exists()
    assert [r["file"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")] == ["one.pdf"]


def test_argv_budget_splits_pages_across_focr_batches(repo_tmp, monkeypatch, capsys):
    """Over the argv budget the pages split into several processes, and
    every PDF still completes."""
    _ocr_two_scans(repo_tmp, monkeypatch)
    monkeypatch.setattr(startup, "_ARGV_CHAR_BUDGET", 1)  # one page per chunk

    commands = []

    def recording_popen(cmd, **kw):
        commands.append(_focr_images(cmd))
        return _fake_focr_success(cmd, **kw)

    monkeypatch.setattr(startup.subprocess, "Popen", recording_popen)

    assert run_main() == 0
    assert "2 converted, 0 unchanged, 0 failed" in capsys.readouterr().out
    assert [len(c) for c in commands] == [1, 1, 1, 1]  # 2 PDFs × 2 pages
    assert (repo_tmp / "one.pdf.md").read_text(encoding="utf-8").count("OCR text for") == 2
    assert (repo_tmp / "two.pdf.md").read_text(encoding="utf-8").count("OCR text for") == 2


def test_chunker_prefers_pdf_boundaries_and_splits_oversized_pdfs():
    pages = {
        "a.pdf": [Path("aaaa"), Path("aaaa")],  # 5 chars each with the separator
        "b.pdf": [Path("bbbb")],
    }
    assert startup._chunk_pages(pages, budget=100) == [
        [Path("aaaa"), Path("aaaa"), Path("bbbb")]
    ]
    # a.pdf alone exceeds the budget → boundary before b.pdf, never inside it
    assert startup._chunk_pages(pages, budget=12) == [
        [Path("aaaa"), Path("aaaa")],
        [Path("bbbb")],
    ]
    # a single PDF larger than the whole budget is split internally
    assert startup._chunk_pages(pages, budget=5) == [
        [Path("aaaa")],
        [Path("aaaa")],
        [Path("bbbb")],
    ]


def test_focr_env_defaults_never_override_the_user(monkeypatch):
    monkeypatch.delenv("FOCR_NO_PROGRESS", raising=False)
    assert startup._focr_env()["FOCR_NO_PROGRESS"] == "1"
    monkeypatch.setenv("FOCR_NO_PROGRESS", "0")
    assert startup._focr_env()["FOCR_NO_PROGRESS"] == "0"


def test_ocr_int8_setting_drives_both_the_env_and_the_flag(monkeypatch):
    """The all-int8 decoder needs the env keys and the flag together."""
    for key in startup._FOCR_INT8_ENV:
        monkeypatch.delenv(key, raising=False)

    monkeypatch.setattr(startup, "OCR_INT8", True)
    env = startup._focr_env()
    assert all(env[key] == "1" for key in startup._FOCR_INT8_ENV)
    assert startup._focr_argv("focr", [Path("p.png")]) == [
        "focr", "ocr-batch", "p.png", "--json", "--experimental-full-int8"
    ]

    monkeypatch.setattr(startup, "OCR_INT8", False)
    env = startup._focr_env()
    assert not any(key in env for key in startup._FOCR_INT8_ENV)
    assert startup._focr_argv("focr", [Path("p.png")]) == [
        "focr", "ocr-batch", "p.png", "--json"
    ]


def test_ocr_int8_false_in_settings_reaches_the_focr_command(
    repo_tmp, monkeypatch, capsys
):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf", pages=1)
    (repo_tmp / "settings.json").write_text(
        json.dumps({"ocr_int8": False}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])
    commands = []

    def recording_popen(cmd, **kw):
        commands.append(list(cmd))
        return _fake_focr_success(cmd, **kw)

    monkeypatch.setattr(startup.subprocess, "Popen", recording_popen)
    assert run_main() == 0
    assert "1 converted" in capsys.readouterr().out
    assert startup._FOCR_INT8_FLAG not in commands[0]


# ---------------------------------------------------------------------------
# Empty tree / reconciliation persistence
# ---------------------------------------------------------------------------


def test_empty_tree_prunes_indexes_and_keeps_sidecars(repo_tmp, capsys):
    (repo_tmp / ".hash_index.csv").write_text(
        "file,hash\ngone.eml,deadbeef\n", encoding="utf-8"
    )
    (repo_tmp / ".token_index.csv").write_text(
        "file,tokens\ngone.eml.md,42\n", encoding="utf-8"
    )
    (repo_tmp / ".ocr_index.csv").write_text(
        "file,hash,pages,pg_image_only,pg_scan_ocr,pg_digital,pg_other,verdict,producer,ocr_done\n"
        "gone.pdf,beef,1,0,0,1,0,digital-text,x,\n",
        encoding="utf-8",
    )
    orphan = repo_tmp / "gone.eml.md"
    orphan.write_text("keep me", encoding="utf-8")

    assert run_main() == 0
    assert "No office documents found." in capsys.readouterr().out
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []
    assert read_csv_dict(repo_tmp / ".token_index.csv") == []
    assert read_csv_dict(repo_tmp / ".ocr_index.csv") == []
    assert (repo_tmp / ".hash_index.csv").read_text(encoding="utf-8").startswith(
        "file,hash\n"
    )
    assert orphan.read_text(encoding="utf-8") == "keep me"


def test_deleted_source_removes_both_candidate_token_rows(repo_tmp):
    write_token_map(repo_tmp, {"gone.eml.md": 1, ".gone.eml.md": 2})
    (repo_tmp / ".hash_index.csv").write_text(
        "file,hash\ngone.eml,deadbeef\n", encoding="utf-8"
    )
    (repo_tmp / "gone.eml.md").write_bytes(b"orphan preferred")
    (repo_tmp / ".gone.eml.md").write_bytes(b"orphan alternate")

    assert run_main() == 0
    assert read_csv_dict(repo_tmp / ".token_index.csv") == []


def test_index_write_failure_exits_nonzero(repo_tmp, monkeypatch, capsys):
    write_eml(repo_tmp / "a.eml")

    original = startup.atomic_write_text

    def targeted_write(path, text, newline=""):
        if path.name == startup.HASH_INDEX_FILENAME:
            raise OSError("injected index write failure")
        return original(path, text, newline=newline)

    monkeypatch.setattr(startup, "atomic_write_text", targeted_write)
    assert run_main() == 1
    assert "writing .hash_index.csv failed" in capsys.readouterr().out
    # token index was still written before the failing hash write
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {"a.eml.md"}
    # hash index never appeared → next run reconverts (conservative)
    assert not (repo_tmp / ".hash_index.csv").exists()


def test_token_write_failure_withholds_certification_marker(repo_tmp, monkeypatch):
    """A failed token write must not be followed by a hash write: the stale
    token row plus a fresh hash would certify a wrong token count forever."""
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    prior_hash_bytes = (repo_tmp / ".hash_index.csv").read_bytes()

    original = startup.atomic_write_text
    writes = []
    ocr_serializations = 0
    original_ocr_serializer = startup.serialize_ocr_index

    def targeted_write(path, text, newline=""):
        writes.append(path.name)
        if path.name == startup.TOKEN_INDEX_FILENAME:
            raise OSError("injected token write failure")
        return original(path, text, newline=newline)

    def hash_should_not_serialize(index):
        raise AssertionError("hash index serialized after an earlier write failure")

    def count_ocr_serialization(index):
        nonlocal ocr_serializations
        ocr_serializations += 1
        return original_ocr_serializer(index)

    monkeypatch.setattr(startup, "atomic_write_text", targeted_write)
    monkeypatch.setattr(startup, "serialize_hash_index", hash_should_not_serialize)
    monkeypatch.setattr(startup, "serialize_ocr_index", count_ocr_serialization)
    errors = startup.persist_indexes(
        repo_tmp,
        {"a.eml": "0badf00d"},
        {"a.eml.md": 999},
        {
            "doc.pdf": {
                "file": "doc.pdf",
                "hash": "beef",
                "verdict": "digital-text",
            }
        },
    )
    # hash index NOT updated: previous certification marker left untouched
    assert (repo_tmp / ".hash_index.csv").read_bytes() == prior_hash_bytes
    joined = " | ".join(errors)
    assert "writing .token_index.csv failed" in joined
    assert "withheld .hash_index.csv write" in joined
    assert writes == [startup.TOKEN_INDEX_FILENAME, startup.OCR_INDEX_FILENAME]
    assert ocr_serializations == 1
    assert not list(repo_tmp.glob(".*.tmp"))


@pytest.mark.parametrize(
    "failed_name", [startup.TOKEN_INDEX_FILENAME, startup.OCR_INDEX_FILENAME]
)
def test_each_index_write_failure_is_reported(
    repo_tmp, monkeypatch, capsys, failed_name
):
    write_eml(repo_tmp / "a.eml")

    original = startup.atomic_write_text

    def targeted_write(path, text, newline=""):
        if path.name == failed_name:
            raise OSError("injected")
        return original(path, text, newline=newline)

    monkeypatch.setattr(startup, "atomic_write_text", targeted_write)
    assert run_main() == 1
    assert "failed" in capsys.readouterr().out


def test_ocr_write_failure_withholds_hash_without_serializing_it(
    repo_tmp, monkeypatch
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    prior_hash = (repo_tmp / startup.HASH_INDEX_FILENAME).read_bytes()
    current_tokens = token_map(repo_tmp)
    original_write = startup.atomic_write_text
    writes = []

    def fail_ocr_write(path, text, newline=""):
        writes.append(path.name)
        if path.name == startup.OCR_INDEX_FILENAME:
            raise OSError("injected OCR-index failure")
        return original_write(path, text, newline=newline)

    def hash_should_not_serialize(index):
        raise AssertionError("hash serialized after OCR-index failure")

    monkeypatch.setattr(startup, "atomic_write_text", fail_ocr_write)
    monkeypatch.setattr(startup, "serialize_hash_index", hash_should_not_serialize)
    errors = startup.persist_indexes(
        repo_tmp,
        {"a.eml": "newhash"},
        current_tokens,
        {
            "doc.pdf": {
                "file": "doc.pdf",
                "hash": "beef",
                "verdict": "digital-text",
            }
        },
    )

    assert (repo_tmp / startup.HASH_INDEX_FILENAME).read_bytes() == prior_hash
    assert writes == [startup.OCR_INDEX_FILENAME]
    assert any("writing .ocr_index.csv failed" in error for error in errors)
    assert any("withheld .hash_index.csv write" in error for error in errors)


def test_persist_indexes_serializes_each_index_exactly_once(
    repo_tmp, monkeypatch
):
    calls = {"token": 0, "ocr": 0, "hash": 0}
    writes = []
    original_token = startup.serialize_token_index
    original_ocr = startup.serialize_ocr_index
    original_hash = startup.serialize_hash_index
    original_write = startup.atomic_write_text

    def count_token(index):
        calls["token"] += 1
        return original_token(index)

    def count_ocr(index):
        calls["ocr"] += 1
        return original_ocr(index)

    def count_hash(index):
        calls["hash"] += 1
        return original_hash(index)

    def record_write(path, text, newline=""):
        writes.append(path.name)
        return original_write(path, text, newline=newline)

    monkeypatch.setattr(startup, "serialize_token_index", count_token)
    monkeypatch.setattr(startup, "serialize_ocr_index", count_ocr)
    monkeypatch.setattr(startup, "serialize_hash_index", count_hash)
    monkeypatch.setattr(startup, "atomic_write_text", record_write)

    assert startup.persist_indexes(
        repo_tmp, {"a.eml": "deadbeef"}, {"a.eml.md": 3}, {}
    ) == []
    assert calls == {"token": 1, "ocr": 1, "hash": 1}
    assert writes == [
        startup.TOKEN_INDEX_FILENAME,
        startup.OCR_INDEX_FILENAME,
        startup.HASH_INDEX_FILENAME,
    ]


def test_shared_index_serializer_preserves_headers_sorting_and_newlines():
    assert startup._serialize_index({"b": 2, "a": 1}, "tokens") == (
        "file,tokens\r\na,1\r\nb,2\r\n"
    )
    assert startup.serialize_hash_index({"b": "bb", "a": "aa"}) == (
        "file,hash\r\na,aa\r\nb,bb\r\n"
    )


# ---------------------------------------------------------------------------
# UTF-8 round trips and naming
# ---------------------------------------------------------------------------


def test_nonascii_filenames_round_trip_through_indexes(repo_tmp, capsys):
    (repo_tmp / "münchen agrément κ").mkdir()
    write_eml(repo_tmp / "münchen agrément κ" / "café statement № 5.eml", subject="Résumé")

    assert run_main() == 0
    capsys.readouterr()
    sidecar = repo_tmp / "münchen agrément κ" / "café statement № 5.eml.md"
    assert sidecar.exists()
    assert "# Résumé" in sidecar.read_text(encoding="utf-8")
    token_rows = read_csv_dict(repo_tmp / ".token_index.csv")
    assert token_rows[0]["file"] == "münchen agrément κ/café statement № 5.eml.md"
    first_tokens = token_rows[0]["tokens"]

    # second run: stable, unchanged, identical token count
    assert run_main() == 0
    assert "0 converted, 1 unchanged" in capsys.readouterr().out
    assert read_csv_dict(repo_tmp / ".token_index.csv")[0]["tokens"] == first_tokens


def test_sidecar_naming_for_sigcheck(repo_tmp, monkeypatch):
    """converted_path builds both styles; other_style_path is the inverse."""
    for name in ("x.pdf", "x.docx", "x.eml", "x.msg", "x.mbox", "x.mht"):
        monkeypatch.setattr(startup, "SIDECAR_DOTFILES", False)
        assert startup.converted_path(repo_tmp / name).name == f"{name}.md"
        assert startup.other_style_path(repo_tmp / name).name == f".{name}.md"
        monkeypatch.setattr(startup, "SIDECAR_DOTFILES", True)
        assert startup.converted_path(repo_tmp / name).name == f".{name}.md"
        assert startup.other_style_path(repo_tmp / name).name == f"{name}.md"
    monkeypatch.setattr(startup, "SIDECAR_DOTFILES", False)
    with pytest.raises(ValueError):
        startup.converted_path(repo_tmp / "x.mbx")
    with pytest.raises(ValueError):
        startup.other_style_path(repo_tmp / "x.mbx")


def test_sidecar_helper_and_wrappers_read_setting_at_call_time(repo_tmp, monkeypatch):
    source = repo_tmp / "x.docx"
    assert startup._sidecar_path(source, dotted=False).name == "x.docx.md"
    assert startup._sidecar_path(source, dotted=True).name == ".x.docx.md"

    monkeypatch.setattr(startup, "SIDECAR_DOTFILES", False)
    assert startup.converted_path(source).name == "x.docx.md"
    monkeypatch.setattr(startup, "SIDECAR_DOTFILES", True)
    assert startup.converted_path(source).name == ".x.docx.md"


def test_caption_cache_cleared_each_run(repo_tmp):
    cache = repo_tmp / "caption_cache"
    cache.mkdir()
    (cache / "leftover.txt").write_text("old", encoding="utf-8")
    (cache / "subdir").mkdir()
    assert run_main() == 0
    assert cache.exists()
    assert list(cache.iterdir()) == []


def test_summary_counts_exact(repo_tmp, monkeypatch, capsys):
    write_eml(repo_tmp / "keep.eml")
    assert run_main() == 0
    capsys.readouterr()

    write_eml(repo_tmp / "new.eml", subject="fresh")
    bad = write_eml(repo_tmp / "unhashable.eml")
    make_scanned_pdf(repo_tmp / "scan.pdf")
    monkeypatch.setattr(startup, "hash_file", _raise_for(bad, startup.hash_file))

    assert run_main() == 1
    out = capsys.readouterr().out
    assert (
        "Done. 4 office documents indexed: 1 converted, 1 unchanged, "
        "1 failed, 1 deferred for OCR." in out
    )


# ---------------------------------------------------------------------------
# Sidecar-style migration (settings.json: sidecar_dotfiles)
# ---------------------------------------------------------------------------


def set_dotfiles(repo_tmp, value: bool) -> None:
    (repo_tmp / "settings.json").write_text(
        json.dumps({"sidecar_dotfiles": value}), encoding="utf-8"
    )


def token_map(repo_tmp) -> dict[str, int]:
    return {
        row["file"]: int(row["tokens"])
        for row in read_csv_dict(repo_tmp / ".token_index.csv")
    }


def write_token_map(repo_tmp, rows: dict[str, int]) -> None:
    (repo_tmp / ".token_index.csv").write_text(
        startup.serialize_token_index(rows), encoding="utf-8"
    )


def preserved_backups(repo_tmp, sidecar_name: str) -> list[Path]:
    return sorted(repo_tmp.glob(f"{sidecar_name}.conflict-preserved-*"))


def test_flip_preference_renames_and_stays_certified(repo_tmp, monkeypatch, capsys):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    sidecar_bytes = (repo_tmp / "a.eml.md").read_bytes()

    set_dotfiles(repo_tmp, True)
    calls = []
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: calls.append(s) or "x")
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: (_ for _ in ()).throw(AssertionError("must trust stored count")),
    )
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 renamed" in out
    assert "0 converted, 1 unchanged" in out
    assert calls == []  # renamed, token key rewritten, still certified
    assert not (repo_tmp / "a.eml.md").exists()
    assert (repo_tmp / ".a.eml.md").read_bytes() == sidecar_bytes
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {".a.eml.md"}

    # flip back → renamed back
    set_dotfiles(repo_tmp, False)
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 renamed" in out and "0 converted, 1 unchanged" in out
    assert (repo_tmp / "a.eml.md").read_bytes() == sidecar_bytes
    assert not (repo_tmp / ".a.eml.md").exists()
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {"a.eml.md"}


def test_both_styles_different_with_one_authoritative_row_preserves_unindexed(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    (repo_tmp / ".a.eml.md").write_text("stale dotfile copy", encoding="utf-8")
    current = (repo_tmp / "a.eml.md").read_bytes()
    write_eml(repo_tmp / "a.eml", subject="changed source must still wait")

    def should_not_run(*args, **kwargs):
        raise AssertionError("resolved authority must skip processing this run")

    monkeypatch.setattr(startup, "convert_to_markdown", should_not_run)
    monkeypatch.setattr(startup, "count_tokens", should_not_run)
    monkeypatch.setattr(startup, "run_ocr", should_not_run)

    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 automatically resolved conflict(s)" in out
    assert (repo_tmp / "a.eml.md").read_bytes() == current
    assert not (repo_tmp / ".a.eml.md").exists()
    backups = preserved_backups(repo_tmp, ".a.eml.md")
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "stale dotfile copy"
    assert not backups[0].name.endswith(".md")
    assert set(token_map(repo_tmp)) == {"a.eml.md"}


def test_both_different_alternate_authority_is_canonicalized_without_processing(
    repo_tmp, monkeypatch
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    authoritative = (repo_tmp / "a.eml.md").read_bytes()
    stored_count = token_map(repo_tmp)["a.eml.md"]
    set_dotfiles(repo_tmp, True)
    (repo_tmp / ".a.eml.md").write_bytes(b"unindexed preferred bytes")

    def should_not_run(*args, **kwargs):
        raise AssertionError("resolved authority must skip processing this run")

    monkeypatch.setattr(startup, "convert_to_markdown", should_not_run)
    monkeypatch.setattr(startup, "count_tokens", should_not_run)
    monkeypatch.setattr(startup, "run_ocr", should_not_run)

    assert run_main() == 0
    assert not (repo_tmp / "a.eml.md").exists()
    assert (repo_tmp / ".a.eml.md").read_bytes() == authoritative
    assert token_map(repo_tmp) == {".a.eml.md": stored_count}
    backups = preserved_backups(repo_tmp, ".a.eml.md")
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"unindexed preferred bytes"


def test_crash_window_repair_rewrites_key_without_reconversion(
    repo_tmp, monkeypatch, capsys
):
    """Sidecar already renamed to the current style but the token row is
    still keyed under the other style (crash between rename and persist)."""
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    set_dotfiles(repo_tmp, True)
    # simulate the crash window: file renamed by hand, index untouched
    (repo_tmp / "a.eml.md").rename(repo_tmp / ".a.eml.md")

    calls = []
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: calls.append(s) or "x")
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: (_ for _ in ()).throw(AssertionError("must trust stored count")),
    )
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 repaired" in out
    assert "0 converted, 1 unchanged" in out
    assert calls == []
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {".a.eml.md"}


def test_inverse_crash_shape_preserves_then_regenerates(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    original = (repo_tmp / "a.eml.md").read_bytes()
    set_dotfiles(repo_tmp, True)
    old_count = token_map(repo_tmp)["a.eml.md"]
    write_token_map(repo_tmp, {".a.eml.md": old_count})
    conversions = []
    original_convert = startup.convert_to_markdown
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: conversions.append(source) or original_convert(source),
    )

    assert run_main() == 0
    out = capsys.readouterr().out
    assert conversions == [repo_tmp / "a.eml"]
    assert "1 converted" in out
    assert (repo_tmp / ".a.eml.md").exists()
    assert not (repo_tmp / "a.eml.md").exists()
    backups = preserved_backups(repo_tmp, "a.eml.md")
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert set(token_map(repo_tmp)) == {".a.eml.md"}


def test_inverse_regeneration_token_write_failure_cannot_stale_certify(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    prior_hash = (repo_tmp / ".hash_index.csv").read_bytes()
    original_sidecar = (repo_tmp / "a.eml.md").read_bytes()
    set_dotfiles(repo_tmp, True)
    write_token_map(repo_tmp, {".a.eml.md": 999})
    original_write = startup.atomic_write_text

    def fail_token_write(path, text, newline=""):
        if path.name == startup.TOKEN_INDEX_FILENAME:
            raise OSError("injected token persistence failure")
        return original_write(path, text, newline=newline)

    monkeypatch.setattr(startup, "atomic_write_text", fail_token_write)
    assert run_main() == 1
    assert "withheld .hash_index.csv write" in capsys.readouterr().out
    # The regenerated output stayed staged and was discarded. The stale row
    # therefore cannot name a physical preferred sidecar on the next run.
    assert not (repo_tmp / ".a.eml.md").exists()
    assert not (repo_tmp / "a.eml.md").exists()
    backups = preserved_backups(repo_tmp, "a.eml.md")
    assert len(backups) == 1 and backups[0].read_bytes() == original_sidecar
    assert token_map(repo_tmp) == {".a.eml.md": 999}
    assert (repo_tmp / ".hash_index.csv").read_bytes() == prior_hash
    assert not list(repo_tmp.glob(".*.tmp"))

    monkeypatch.setattr(startup, "atomic_write_text", original_write)
    conversions = []
    original_convert = startup.convert_to_markdown
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: conversions.append(source) or original_convert(source),
    )
    assert run_main() == 0
    assert conversions == [repo_tmp / "a.eml"]
    assert (repo_tmp / ".a.eml.md").exists()
    assert token_map(repo_tmp)[".a.eml.md"] != 999


def test_single_unindexed_sidecar_preserved_before_conversion(
    repo_tmp, monkeypatch
):
    source = write_eml(repo_tmp / "a.eml")
    unindexed = repo_tmp / "a.eml.md"
    unindexed.write_bytes(b"user bytes that must survive")
    original_convert = startup.convert_to_markdown
    saw_backup = []

    def convert(path):
        backups = preserved_backups(repo_tmp, "a.eml.md")
        assert not unindexed.exists()
        assert len(backups) == 1
        saw_backup.append(backups[0].read_bytes())
        return original_convert(path)

    monkeypatch.setattr(startup, "convert_to_markdown", convert)
    assert run_main() == 0
    assert saw_backup == [b"user bytes that must survive"]
    assert (repo_tmp / "a.eml.md").exists()
    assert source.exists()


def test_both_identical_one_authoritative_row_collapses_to_preferred(
    repo_tmp, monkeypatch
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    alternate.write_bytes(preferred.read_bytes())
    stored_count = token_map(repo_tmp)["a.eml.md"]
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: (_ for _ in ()).throw(AssertionError("must trust token row")),
    )

    assert run_main() == 0
    assert preferred.exists() and not alternate.exists()
    assert token_map(repo_tmp) == {"a.eml.md": stored_count}
    assert len(preserved_backups(repo_tmp, ".a.eml.md")) == 1

    before = _index_mtimes(repo_tmp)
    time.sleep(0.02)
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: (_ for _ in ()).throw(AssertionError("second run must be stable")),
    )
    assert run_main() == 0
    assert _index_mtimes(repo_tmp) == before


def test_both_identical_two_agreeing_rows_collapse(repo_tmp, monkeypatch):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    alternate.write_bytes(preferred.read_bytes())
    stored_count = token_map(repo_tmp)["a.eml.md"]
    write_token_map(
        repo_tmp, {"a.eml.md": stored_count, ".a.eml.md": stored_count}
    )
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: (_ for _ in ()).throw(AssertionError("must trust token rows")),
    )

    assert run_main() == 0
    assert preferred.exists() and not alternate.exists()
    assert token_map(repo_tmp) == {"a.eml.md": stored_count}
    assert len(preserved_backups(repo_tmp, ".a.eml.md")) == 1


def test_both_identical_no_rows_retokenizes_matching_hash(repo_tmp, monkeypatch):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    alternate.write_bytes(preferred.read_bytes())
    (repo_tmp / ".token_index.csv").write_text("file,tokens\n", encoding="utf-8")
    calls = []
    original_count = startup.count_tokens
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: calls.append(path) or original_count(path),
    )
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: (_ for _ in ()).throw(AssertionError("must retokenize")),
    )

    assert run_main() == 0
    assert calls == [preferred]
    assert preferred.exists() and not alternate.exists()
    assert set(token_map(repo_tmp)) == {"a.eml.md"}
    assert len(preserved_backups(repo_tmp, ".a.eml.md")) == 1


def test_both_identical_no_rows_without_matching_hash_preserves_and_regenerates(
    repo_tmp, monkeypatch
):
    source = write_eml(repo_tmp / "a.eml")
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    preferred.write_bytes(b"same unindexed bytes")
    alternate.write_bytes(preferred.read_bytes())
    original_convert = startup.convert_to_markdown
    conversions = []
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda path: conversions.append(path) or original_convert(path),
    )

    assert run_main() == 0
    assert conversions == [source]
    backups = preserved_backups(repo_tmp, "a.eml.md")
    assert len(backups) == 1 and backups[0].read_bytes() == b"same unindexed bytes"
    assert preferred.exists() and not alternate.exists()
    assert preserved_backups(repo_tmp, ".a.eml.md") == []
    assert set(token_map(repo_tmp)) == {"a.eml.md"}


def test_one_file_with_own_row_discards_missing_candidate_row(repo_tmp):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    stored_count = token_map(repo_tmp)["a.eml.md"]
    write_token_map(
        repo_tmp, {"a.eml.md": stored_count, ".a.eml.md": stored_count + 5}
    )

    assert run_main() == 0
    assert token_map(repo_tmp) == {"a.eml.md": stored_count}


def test_both_identical_two_disagreeing_rows_are_unresolved(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    alternate.write_bytes(preferred.read_bytes())
    before_preferred = preferred.read_bytes()
    write_token_map(repo_tmp, {"a.eml.md": 11, ".a.eml.md": 12})
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: (_ for _ in ()).throw(AssertionError("conflict excluded")),
    )

    assert run_main() == 1
    out = capsys.readouterr().out
    assert "a.eml.md and .a.eml.md" in out
    assert "token rows disagree" in out
    assert preferred.read_bytes() == alternate.read_bytes() == before_preferred
    assert token_map(repo_tmp) == {"a.eml.md": 11, ".a.eml.md": 12}


def test_both_different_two_authoritative_rows_are_unresolved(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    preferred_before = preferred.read_bytes()
    alternate.write_bytes(b"different indexed bytes")
    write_token_map(repo_tmp, {"a.eml.md": 10, ".a.eml.md": 20})
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: (_ for _ in ()).throw(AssertionError("conflict excluded")),
    )
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: (_ for _ in ()).throw(AssertionError("conflict excluded")),
    )

    assert run_main() == 1
    out = capsys.readouterr().out
    assert "a.eml.md and .a.eml.md" in out
    assert "byte-different" in out
    assert preferred.read_bytes() == preferred_before
    assert alternate.read_bytes() == b"different indexed bytes"
    assert token_map(repo_tmp) == {"a.eml.md": 10, ".a.eml.md": 20}


def test_new_unresolved_conflict_is_not_hash_certified(
    repo_tmp, monkeypatch
):
    write_eml(repo_tmp / "a.eml")
    (repo_tmp / "a.eml.md").write_bytes(b"first indexed artifact")
    (repo_tmp / ".a.eml.md").write_bytes(b"second indexed artifact")
    write_token_map(repo_tmp, {"a.eml.md": 1, ".a.eml.md": 2})
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: (_ for _ in ()).throw(AssertionError("conflict excluded")),
    )

    assert run_main() == 1
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []
    assert token_map(repo_tmp) == {"a.eml.md": 1, ".a.eml.md": 2}


def test_both_different_no_rows_preserved_then_regenerated(
    repo_tmp, monkeypatch
):
    source = write_eml(repo_tmp / "a.eml")
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    preferred.write_bytes(b"first unique bytes")
    alternate.write_bytes(b"second unique bytes")
    seen = []
    original_convert = startup.convert_to_markdown

    def convert(path):
        assert not preferred.exists() and not alternate.exists()
        seen.extend(
            backup.read_bytes()
            for name in ("a.eml.md", ".a.eml.md")
            for backup in preserved_backups(repo_tmp, name)
        )
        return original_convert(path)

    monkeypatch.setattr(startup, "convert_to_markdown", convert)
    assert run_main() == 0
    assert source.exists() and preferred.exists() and not alternate.exists()
    assert set(seen) == {b"first unique bytes", b"second unique bytes"}
    assert set(token_map(repo_tmp)) == {"a.eml.md"}


def test_two_phase_preservation_failure_leaves_both_candidates(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    preferred.write_bytes(b"first")
    alternate.write_bytes(b"second")
    original_copy = startup._copy_to_unique_backup
    calls = 0

    def fail_second(path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected preservation failure")
        return original_copy(path)

    monkeypatch.setattr(startup, "_copy_to_unique_backup", fail_second)
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: (_ for _ in ()).throw(AssertionError("conflict excluded")),
    )

    assert run_main() == 1
    assert "preservation failure" in capsys.readouterr().out
    assert preferred.read_bytes() == b"first"
    assert alternate.read_bytes() == b"second"
    assert preserved_backups(repo_tmp, "a.eml.md") == []
    assert preserved_backups(repo_tmp, ".a.eml.md") == []


def test_conflict_backup_retries_exclusive_name_without_clobber(
    repo_tmp, monkeypatch
):
    candidate = repo_tmp / "a.eml.md"
    candidate.write_bytes(b"candidate bytes")
    collision = repo_tmp / "a.eml.md.conflict-preserved-first"
    collision.write_bytes(b"existing backup")
    values = iter(["first", "second"])

    class FakeUUID:
        def __init__(self, value):
            self.hex = value

    monkeypatch.setattr(
        startup.uuid, "uuid4", lambda: FakeUUID(next(values))
    )
    backup = startup._copy_to_unique_backup(candidate)

    assert backup.name == "a.eml.md.conflict-preserved-second"
    assert backup.read_bytes() == b"candidate bytes"
    assert collision.read_bytes() == b"existing backup"
    assert candidate.read_bytes() == b"candidate bytes"


def test_unhashable_preference_flip_retains_then_recovers(
    repo_tmp, monkeypatch, capsys
):
    source = write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    original_bytes = (repo_tmp / "a.eml.md").read_bytes()
    stored_count = token_map(repo_tmp)["a.eml.md"]
    set_dotfiles(repo_tmp, True)
    original_hash = startup.hash_file
    monkeypatch.setattr(startup, "hash_file", _raise_for(source, original_hash))

    assert run_main() == 1
    assert (repo_tmp / "a.eml.md").read_bytes() == original_bytes
    assert not (repo_tmp / ".a.eml.md").exists()
    assert token_map(repo_tmp) == {"a.eml.md": stored_count}

    monkeypatch.setattr(startup, "hash_file", original_hash)
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: (_ for _ in ()).throw(AssertionError("must trust stored count")),
    )
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 renamed" in out and "0 converted, 1 unchanged" in out
    assert (repo_tmp / ".a.eml.md").read_bytes() == original_bytes
    assert token_map(repo_tmp) == {".a.eml.md": stored_count}


def test_canonical_rename_failure_is_unresolved_and_preserves_authority(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    authoritative = (repo_tmp / "a.eml.md").read_bytes()
    stored_count = token_map(repo_tmp)["a.eml.md"]
    set_dotfiles(repo_tmp, True)
    original_replace = startup.os.replace

    def targeted_replace(source, destination):
        if Path(source).name == "a.eml.md" and Path(destination).name == ".a.eml.md":
            raise OSError("injected rename failure")
        return original_replace(source, destination)

    monkeypatch.setattr(startup.os, "replace", targeted_replace)
    assert run_main() == 1
    assert "injected rename failure" in capsys.readouterr().out
    assert (repo_tmp / "a.eml.md").read_bytes() == authoritative
    assert not (repo_tmp / ".a.eml.md").exists()
    assert token_map(repo_tmp) == {"a.eml.md": stored_count}


def test_unhashable_both_file_conflict_is_reported_without_mutation(
    repo_tmp, monkeypatch, capsys
):
    source = write_eml(repo_tmp / "a.eml")
    preferred = repo_tmp / "a.eml.md"
    alternate = repo_tmp / ".a.eml.md"
    preferred.write_bytes(b"preferred")
    alternate.write_bytes(b"alternate")
    write_token_map(repo_tmp, {"a.eml.md": 1, ".a.eml.md": 2})
    monkeypatch.setattr(startup, "hash_file", _raise_for(source, startup.hash_file))

    assert run_main() == 1
    out = capsys.readouterr().out
    assert "could not be hashed" in out
    assert "a.eml.md and .a.eml.md" in out
    assert preferred.read_bytes() == b"preferred"
    assert alternate.read_bytes() == b"alternate"
    assert token_map(repo_tmp) == {"a.eml.md": 1, ".a.eml.md": 2}


def test_malformed_repo_settings_aborts_before_touching_anything(repo_tmp, capsys):
    write_eml(repo_tmp / "a.eml")
    (repo_tmp / "settings.json").write_text('{"sidecar_dotfiles": "true"}', encoding="utf-8")
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "invalid repo settings" in out
    assert not (repo_tmp / "a.eml.md").exists()
    assert not (repo_tmp / ".hash_index.csv").exists()


# ---------------------------------------------------------------------------
# OCR invariant: needs-OCR PDFs never reach the generic converter
# ---------------------------------------------------------------------------


def _ocr_convert_scan(repo_tmp, monkeypatch, capsys):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf", pages=2)
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])
    monkeypatch.setattr(startup.subprocess, "Popen", _fake_focr_success)
    assert run_main() == 0
    monkeypatch.setattr(sys, "argv", ["startup.py"])
    capsys.readouterr()
    return (repo_tmp / "scan.pdf.md").read_bytes()


def test_ocr_sidecar_with_lost_rows_is_recertified_not_reconverted(
    repo_tmp, monkeypatch, capsys
):
    sidecar_bytes = _ocr_convert_scan(repo_tmp, monkeypatch, capsys)
    # drop BOTH the token row and the hash row: uncertified, and the
    # migration retokenize path (which needs the hash row) can't fire either
    (repo_tmp / ".token_index.csv").write_text("file,tokens\n", encoding="utf-8")
    (repo_tmp / ".hash_index.csv").write_text("file,hash\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: calls.append(s) or "x")
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "0 converted, 1 unchanged, 0 failed, 0 deferred for OCR" in out
    assert calls == []  # never AnyDoc'd
    assert (repo_tmp / "scan.pdf.md").read_bytes() == sidecar_bytes  # byte-identical
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {"scan.pdf.md"}
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".hash_index.csv")} == {"scan.pdf"}
    assert read_csv_dict(repo_tmp / ".ocr_index.csv")[0]["ocr_done"] == "true"


def test_ocr_sidecar_deleted_token_row_restored_via_migration(
    repo_tmp, monkeypatch, capsys
):
    """Token row alone missing: repaired (re-tokenized), sidecar untouched."""
    sidecar_bytes = _ocr_convert_scan(repo_tmp, monkeypatch, capsys)
    (repo_tmp / ".token_index.csv").write_text("file,tokens\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: calls.append(s) or "x")
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "0 converted, 1 unchanged" in out
    assert calls == []
    assert (repo_tmp / "scan.pdf.md").read_bytes() == sidecar_bytes
    assert {r["file"] for r in read_csv_dict(repo_tmp / ".token_index.csv")} == {"scan.pdf.md"}


def test_needs_ocr_lost_done_does_not_retokenize_or_certify(
    repo_tmp, monkeypatch, capsys
):
    sidecar_bytes = _ocr_convert_scan(repo_tmp, monkeypatch, capsys)
    prior_hash = (repo_tmp / ".hash_index.csv").read_bytes()
    (repo_tmp / ".token_index.csv").write_text("file,tokens\n", encoding="utf-8")
    ocr_index = startup.load_ocr_index(repo_tmp)
    ocr_index["scan.pdf"]["ocr_done"] = ""
    startup.save_ocr_index(repo_tmp, ocr_index)
    monkeypatch.setattr(
        startup,
        "count_tokens",
        lambda path: (_ for _ in ()).throw(
            AssertionError("lost OCR authority must not be retokenized")
        ),
    )
    converter_calls = []
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: converter_calls.append(source) or "wrong route",
    )

    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 deferred for OCR" in out
    assert converter_calls == []
    assert not (repo_tmp / "scan.pdf.md").exists()
    backups = preserved_backups(repo_tmp, "scan.pdf.md")
    assert len(backups) == 1 and backups[0].read_bytes() == sidecar_bytes
    assert read_csv_dict(repo_tmp / ".token_index.csv") == []
    assert (repo_tmp / ".hash_index.csv").read_bytes() == prior_hash


def test_single_unindexed_needs_ocr_sidecar_preserved_before_authorized_ocr(
    repo_tmp, monkeypatch
):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf")
    unindexed = repo_tmp / "scan.pdf.md"
    unindexed.write_bytes(b"unindexed scan notes")
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])
    monkeypatch.setattr(startup.subprocess, "Popen", _fake_focr_success)

    assert run_main() == 0
    backups = preserved_backups(repo_tmp, "scan.pdf.md")
    assert len(backups) == 1
    assert backups[0].read_bytes() == b"unindexed scan notes"
    assert unindexed.exists()
    assert unindexed.read_bytes() != b"unindexed scan notes"
    assert set(token_map(repo_tmp)) == {"scan.pdf.md"}


def test_both_unindexed_needs_ocr_files_preserved_then_deferred(
    repo_tmp, monkeypatch, capsys
):
    make_scanned_pdf(repo_tmp / "scan.pdf")
    assert run_main() == 0
    capsys.readouterr()
    preferred = repo_tmp / "scan.pdf.md"
    alternate = repo_tmp / ".scan.pdf.md"
    preferred.write_bytes(b"first scan bytes")
    alternate.write_bytes(b"second scan bytes")
    monkeypatch.setattr(
        startup,
        "convert_to_markdown",
        lambda source: (_ for _ in ()).throw(AssertionError("OCR source routed wrong")),
    )

    assert run_main() == 0
    assert "1 deferred for OCR" in capsys.readouterr().out
    assert not preferred.exists() and not alternate.exists()
    assert preserved_backups(repo_tmp, "scan.pdf.md")[0].read_bytes() == b"first scan bytes"
    assert preserved_backups(repo_tmp, ".scan.pdf.md")[0].read_bytes() == b"second scan bytes"
    assert read_csv_dict(repo_tmp / ".token_index.csv") == []


def test_ocr_flagged_pdf_with_missing_sidecar_is_deferred_never_converted(
    repo_tmp, monkeypatch, capsys
):
    _ocr_convert_scan(repo_tmp, monkeypatch, capsys)
    (repo_tmp / "scan.pdf.md").unlink()

    calls = []
    monkeypatch.setattr(startup, "convert_to_markdown", lambda s: calls.append(s) or "x")
    assert run_main() == 0
    out = capsys.readouterr().out
    assert "1 deferred for OCR" in out
    assert "0 converted" in out
    assert calls == []
    assert not (repo_tmp / "scan.pdf.md").exists()


# ---------------------------------------------------------------------------
# persist_indexes skip-write
# ---------------------------------------------------------------------------


def _index_mtimes(repo_tmp):
    return {
        name: (repo_tmp / name).stat().st_mtime_ns
        for name in (".hash_index.csv", ".token_index.csv", ".ocr_index.csv")
    }


def test_unchanged_second_run_rewrites_no_indexes(repo_tmp, capsys):
    write_eml(repo_tmp / "a.eml")
    make_digital_pdf(repo_tmp / "doc.pdf")
    assert run_main() == 0
    before = _index_mtimes(repo_tmp)
    time.sleep(0.02)
    assert run_main() == 0
    assert _index_mtimes(repo_tmp) == before  # nothing rewritten


def test_one_changed_file_rewrites_only_affected_indexes_hash_last(repo_tmp, capsys):
    write_eml(repo_tmp / "a.eml")
    make_digital_pdf(repo_tmp / "doc.pdf")
    assert run_main() == 0
    before = _index_mtimes(repo_tmp)
    time.sleep(0.02)
    write_eml(repo_tmp / "a.eml", subject="changed subject line")
    assert run_main() == 0
    after = _index_mtimes(repo_tmp)
    assert after[".ocr_index.csv"] == before[".ocr_index.csv"]  # untouched
    assert after[".token_index.csv"] > before[".token_index.csv"]
    assert after[".hash_index.csv"] > before[".hash_index.csv"]
    # certification marker still written last
    assert after[".hash_index.csv"] >= after[".token_index.csv"]


# ---------------------------------------------------------------------------
# .gitignore coverage for every supported suffix (both sidecar styles)
# ---------------------------------------------------------------------------


def test_gitignore_covers_all_source_suffixes():
    import shutil as _shutil
    import subprocess as _subprocess

    from document_conversion import MBX_SUFFIX, SOURCE_SUFFIXES

    if _shutil.which("git") is None:
        pytest.skip("git not available")
    repo_root = Path(__file__).resolve().parent.parent
    names = [f"x{MBX_SUFFIX}"]
    for suffix in SOURCE_SUFFIXES:
        names += [f"x{suffix}", f"x{suffix}.md", f".x{suffix}.md"]
    proc = _subprocess.run(
        ["git", "-C", str(repo_root), "check-ignore", "--no-index", "--verbose", *names],
        capture_output=True,
        text=True,
    )
    ignored = {line.rsplit("\t", 1)[-1] for line in proc.stdout.splitlines()}
    missing = [name for name in names if name not in ignored]
    assert not missing, f"not gitignored: {missing}"
