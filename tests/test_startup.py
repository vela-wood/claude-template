"""Orchestration tests for startup.py: discovery, hashing, concurrency,
status aggregation, staging, persistence, cleanup, and exit codes.

Failures are injected deterministically via monkeypatching rather than
relying on operating-system permission behavior.
"""

import csv
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
        return list(csv.DictReader(l for l in f if not l.startswith("#")))


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


def test_missing_token_row_triggers_reconversion(repo_tmp, capsys):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    # Remove the token row: sidecar+hash alone must not certify unchanged
    (repo_tmp / ".token_index.csv").write_text("file,tokens\n", encoding="utf-8")
    assert run_main() == 0
    assert "1 converted, 0 unchanged" in capsys.readouterr().out


def test_invalid_token_row_triggers_reconversion(repo_tmp, capsys):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    (repo_tmp / ".token_index.csv").write_text(
        "file,tokens\na.eml.md,notanumber\n", encoding="utf-8"
    )
    assert run_main() == 0
    assert "1 converted, 0 unchanged" in capsys.readouterr().out


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
# Converter schema versioning
# ---------------------------------------------------------------------------


def test_pre_migration_hash_index_reads_as_stale_and_reconverts(repo_tmp, capsys):
    """A .hash_index.csv written by main (plain file,hash rows, no schema
    marker) must not certify anything, even when hashes match exactly."""
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    # Rewrite the index in the old main format: same rows, marker stripped
    index = repo_tmp / ".hash_index.csv"
    old_format = "".join(
        line
        for line in index.read_text(encoding="utf-8").splitlines(keepends=True)
        if not line.startswith("#")
    )
    index.write_text(old_format, encoding="utf-8", newline="")

    assert startup.load_hash_index(repo_tmp) == {}
    assert run_main() == 0
    assert "1 converted, 0 unchanged" in capsys.readouterr().out


def test_current_schema_writes_marker_and_certifies_second_run(repo_tmp, capsys):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    first_line = (
        (repo_tmp / ".hash_index.csv").read_text(encoding="utf-8").splitlines()[0]
    )
    assert first_line == f"#schema={startup.CONVERTER_SCHEMA_VERSION}"
    assert run_main() == 0
    assert "0 converted, 1 unchanged" in capsys.readouterr().out


def test_bumped_schema_version_invalidates_certification(
    repo_tmp, monkeypatch, capsys
):
    write_eml(repo_tmp / "a.eml")
    assert run_main() == 0
    capsys.readouterr()
    monkeypatch.setattr(
        startup, "CONVERTER_SCHEMA_VERSION", startup.CONVERTER_SCHEMA_VERSION + 1
    )
    assert run_main() == 0
    assert "1 converted, 0 unchanged" in capsys.readouterr().out
    # after one run under the bumped version, certification works again
    assert run_main() == 0
    assert "0 converted, 1 unchanged" in capsys.readouterr().out


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


def _fake_focr_success(cmd, **kwargs):
    class Proc:
        returncode = 0
        stdout = json.dumps(
            {
                "results": [
                    {"image": arg, "ok": True, "markdown": f"OCR text for {Path(arg).name}"}
                    for arg in cmd
                    if arg.endswith(".png")
                ]
            }
        )

    return Proc()


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


def test_requested_ocr_success_certifies_exactly_once(repo_tmp, monkeypatch, capsys):
    import sys

    make_scanned_pdf(repo_tmp / "scan.pdf", pages=2)
    monkeypatch.setattr(sys, "argv", ["startup.py", "--ocr"])
    calls = []
    monkeypatch.setattr(
        startup, "convert_to_markdown", lambda s: calls.append(s) or "x"
    )
    monkeypatch.setattr(startup.subprocess, "run", _fake_focr_success)

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

    def fake_focr_fail(cmd, **kwargs):
        class Proc:
            returncode = 3
            stdout = ""

        return Proc()

    monkeypatch.setattr(startup.subprocess, "run", fake_focr_fail)
    assert run_main() == 1
    out = capsys.readouterr().out
    assert "0 converted, 0 unchanged, 1 failed" in out
    assert calls == []
    assert not (repo_tmp / "scan.pdf.md").exists()
    assert read_csv_dict(repo_tmp / ".ocr_index.csv")[0]["ocr_done"] == ""
    assert read_csv_dict(repo_tmp / ".hash_index.csv") == []


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
    # schema marker and headers still written
    assert (repo_tmp / ".hash_index.csv").read_text(encoding="utf-8").startswith(
        f"#schema={startup.CONVERTER_SCHEMA_VERSION}\nfile,hash"
    )
    assert orphan.read_text(encoding="utf-8") == "keep me"


def test_index_write_failure_exits_nonzero(repo_tmp, monkeypatch, capsys):
    write_eml(repo_tmp / "a.eml")

    def boom(root, index):
        raise OSError("injected index write failure")

    monkeypatch.setattr(startup, "save_hash_index", boom)
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

    def boom(root, index):
        raise OSError("injected token write failure")

    monkeypatch.setattr(startup, "save_token_index", boom)
    errors = startup.persist_indexes(
        repo_tmp, {"a.eml": "0badf00d"}, {"a.eml.md": 999}, {}
    )
    # hash index NOT updated: previous certification marker left untouched
    assert (repo_tmp / ".hash_index.csv").read_bytes() == prior_hash_bytes
    joined = " | ".join(errors)
    assert "writing .token_index.csv failed" in joined
    assert "withheld .hash_index.csv write" in joined


@pytest.mark.parametrize("which", ["save_token_index", "save_ocr_index"])
def test_each_index_write_failure_is_reported(repo_tmp, monkeypatch, capsys, which):
    write_eml(repo_tmp / "a.eml")

    def boom(root, index):
        raise OSError("injected")

    monkeypatch.setattr(startup, which, boom)
    assert run_main() == 1
    assert "failed" in capsys.readouterr().out


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


def test_sidecar_naming_for_sigcheck(repo_tmp):
    """/sigcheck consumes <name>.<ext>.md sidecar naming; keep it stable."""
    for name in ("x.pdf", "x.docx", "x.eml", "x.msg", "x.mbox", "x.mht"):
        assert startup.converted_path(repo_tmp / name).name == f"{name}.md"
    with pytest.raises(ValueError):
        startup.converted_path(repo_tmp / "x.mbx")


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
