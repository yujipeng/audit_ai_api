"""run-record.json schema v1 lock test — TES-128 S5-B (schema owner).

The audit-suite orchestrator writes one run-record.json per top-level run.
Schema v1 is consumed by S5-C (report rendering) and S5-D (history
aggregation); any breaking change must bump `schema_version` so downstream
consumers can fail-fast on unknown versions.

This file is the single source of truth for the v1 contract. Adding a field
should not require touching this test (the field set is checked as a SUPERSET
of the locked v1 set on the consumer side); REMOVING or RENAMING a field MUST
also bump the version constant — otherwise this test will trip and the change
will not land.

Concretely, the constants below are frozen. If you find yourself editing
LOCKED_TOPLEVEL_FIELDS or LOCKED_CELL_FIELDS in the same commit as the schema,
stop and bump SCHEMA_VERSION instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orchestration.run_record import (
    SCHEMA_VERSION,
    RunRecordWriter,
    new_cell,
    new_record,
)


# ---- Frozen schema v1 surface ----------------------------------------------

# DO NOT EDIT without bumping SCHEMA_VERSION (see module docstring).
LOCKED_SCHEMA_VERSION = 1

LOCKED_TOPLEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "tool",
        "generated_at",
        "elapsed_seconds",
        "code_version",
        "config_digest",
        "redacted_key_ids",
        "cells",
        "summary",
    }
)

LOCKED_CELL_FIELDS = frozenset(
    {
        "step",
        "endpoint",
        "model",
        "status",
        "started_at",
        "finished_at",
        "latency_ms",
        "cache_hit",          # PJM-mandated; design §2.4 R9 mitigation
        "redacted_key_id",
        "schema_version",     # step-local schema version (independent of run-record version)
        "code_version",       # step adapter code version (cache invalidation lever)
        "payload",
        "error",
    }
)

LOCKED_SUMMARY_FIELDS = frozenset(
    {
        "total",
        "ok",
        "error",
        "cache_hits",
        "cache_misses",
        "hit_rate",
    }
)


# ---- Schema constant -------------------------------------------------------

def test_schema_version_constant_is_one():
    assert SCHEMA_VERSION == LOCKED_SCHEMA_VERSION


# ---- new_record / new_cell shape ------------------------------------------

def test_new_record_contains_exactly_v1_toplevel_fields():
    rec = new_record(code_version="abc1234", config_digest="d" * 64)
    assert set(rec.keys()) == LOCKED_TOPLEVEL_FIELDS, (
        f"top-level fields drifted from v1 lock; if intentional, "
        f"bump SCHEMA_VERSION. unexpected={set(rec.keys()) ^ LOCKED_TOPLEVEL_FIELDS}"
    )


def test_new_record_schema_version_matches_constant():
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    assert rec["schema_version"] == SCHEMA_VERSION


def test_new_record_generated_at_is_iso8601_utc():
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    # Must end with +00:00 or Z (timezone-aware UTC).
    assert rec["generated_at"].endswith("+00:00") or rec["generated_at"].endswith("Z")


def test_new_cell_contains_exactly_v1_fields():
    cell = new_cell(
        step="probe",
        endpoint="ep-a",
        model="claude-haiku-4-5",
        redacted_key_id="abcd1234",
        schema_version=1,
        code_version="abc1234",
    )
    assert set(cell.keys()) == LOCKED_CELL_FIELDS, (
        f"cell fields drifted; unexpected={set(cell.keys()) ^ LOCKED_CELL_FIELDS}"
    )


def test_new_cell_default_status_is_pending():
    cell = new_cell(
        step="probe", endpoint="ep-a", model="m", redacted_key_id="x",
        schema_version=1, code_version="abc1234",
    )
    assert cell["status"] == "pending"
    assert cell["cache_hit"] is False


# ---- summary -------------------------------------------------------------

def test_summary_contains_exactly_v1_fields():
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    assert set(rec["summary"].keys()) == LOCKED_SUMMARY_FIELDS


# ---- writer round-trip ---------------------------------------------------

def test_writer_emits_valid_json(tmp_path: Path):
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    cell = new_cell(
        step="probe", endpoint="ep-a", model="m", redacted_key_id="y",
        schema_version=1, code_version="abc1234",
    )
    cell["status"] = "ok"
    cell["cache_hit"] = True
    cell["latency_ms"] = 12.5
    rec["cells"].append(cell)

    w = RunRecordWriter(tmp_path / "run-record.json")
    w.write(rec, elapsed_seconds=1.23)

    raw = (tmp_path / "run-record.json").read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert parsed["schema_version"] == 1
    assert parsed["elapsed_seconds"] == pytest.approx(1.23)
    assert len(parsed["cells"]) == 1
    assert parsed["cells"][0]["cache_hit"] is True
    assert parsed["cells"][0]["status"] == "ok"


def test_writer_recomputes_summary(tmp_path: Path):
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    for i, status in enumerate(("ok", "ok", "ok", "error")):
        cell = new_cell(
            step="probe", endpoint=f"ep-{i}", model="m",
            redacted_key_id="z", schema_version=1, code_version="abc1234",
        )
        cell["status"] = status
        cell["cache_hit"] = i < 2  # first two are cached
        rec["cells"].append(cell)

    w = RunRecordWriter(tmp_path / "out.json")
    w.write(rec, elapsed_seconds=0.5)

    parsed = json.loads((tmp_path / "out.json").read_text())
    s = parsed["summary"]
    assert s["total"] == 4
    assert s["ok"] == 3
    assert s["error"] == 1
    assert s["cache_hits"] == 2
    assert s["cache_misses"] == 2
    assert s["hit_rate"] == pytest.approx(0.5)


def test_writer_refuses_to_write_records_with_wrong_schema_version(tmp_path: Path):
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    rec["schema_version"] = 999  # downstream forgery / version mismatch
    w = RunRecordWriter(tmp_path / "x.json")
    with pytest.raises(ValueError, match="schema_version"):
        w.write(rec, elapsed_seconds=0.0)


def test_writer_refuses_to_write_unknown_toplevel_keys(tmp_path: Path):
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    rec["i_added_a_field_without_bumping_version"] = "bad"
    w = RunRecordWriter(tmp_path / "x.json")
    with pytest.raises(ValueError, match="unknown top-level"):
        w.write(rec, elapsed_seconds=0.0)


def test_writer_refuses_to_write_unknown_cell_keys(tmp_path: Path):
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    cell = new_cell(
        step="probe", endpoint="e", model="m", redacted_key_id="r",
        schema_version=1, code_version="abc1234",
    )
    cell["surprise"] = "bad"
    rec["cells"].append(cell)
    w = RunRecordWriter(tmp_path / "x.json")
    with pytest.raises(ValueError, match="unknown cell"):
        w.write(rec, elapsed_seconds=0.0)


def test_writer_zero_cells_yields_zero_hit_rate(tmp_path: Path):
    rec = new_record(code_version="abc1234", config_digest="x" * 64)
    w = RunRecordWriter(tmp_path / "x.json")
    w.write(rec, elapsed_seconds=0.0)
    parsed = json.loads((tmp_path / "x.json").read_text())
    assert parsed["summary"]["hit_rate"] == 0.0
    assert parsed["summary"]["total"] == 0
