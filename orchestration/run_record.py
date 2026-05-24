"""run-record.json schema v1 — the audit-matrix orchestrator's authoritative output.

This module is the **schema owner** for run-record.json. Downstream consumers
(S5-C report renderer, S5-D history aggregator) MUST treat the field set here
as load-bearing and fail-fast on `schema_version != 1`.

Cross-Story invariants enforced here:

* `SCHEMA_VERSION` is a runtime-checked constant. Bumping it is a deliberate
  breaking-change signal — `tests/test_run_record_schema.py` is the gate.
* `RunRecordWriter.write()` validates the in-memory dict before serialisation:
  any unknown top-level or per-cell key trips a ValueError. This is the
  defense against "downstream silently grows a field that nobody else can
  decode" — the breakage surfaces at write time, on the producer, where the
  authoring dev still has context.
* Every cell carries `cache_hit` (design §2.4 R9 mitigation) and a
  `redacted_key_id` rather than any raw credential material. The redacted-id
  comes from `security.bearer.CredentialBearer.redacted_id`.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

SCHEMA_VERSION = 1

TOOL_NAME = "audit-matrix"

_TOPLEVEL_FIELDS = (
    "schema_version",
    "tool",
    "generated_at",
    "elapsed_seconds",
    "code_version",
    "config_digest",
    "redacted_key_ids",
    "cells",
    "summary",
)

_CELL_FIELDS = (
    "step",
    "endpoint",
    "model",
    "status",
    "started_at",
    "finished_at",
    "latency_ms",
    "cache_hit",
    "redacted_key_id",
    "schema_version",
    "code_version",
    "payload",
    "error",
)

_SUMMARY_FIELDS = ("total", "ok", "error", "cache_hits", "cache_misses", "hit_rate")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_record(
    *, code_version: str, config_digest: str,
    redacted_key_ids: List[str] | None = None,
) -> Dict[str, Any]:
    """Build a fresh run-record skeleton.

    The returned dict has the full v1 field set populated with defaults. The
    caller appends cells and the writer finalises the summary at write time.
    """
    return {
        "schema_version": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "generated_at": _utc_now_iso(),
        "elapsed_seconds": 0.0,
        "code_version": code_version,
        "config_digest": config_digest,
        "redacted_key_ids": list(redacted_key_ids or []),
        "cells": [],
        "summary": {k: 0 for k in _SUMMARY_FIELDS} | {"hit_rate": 0.0},
    }


def new_cell(
    *,
    step: str,
    endpoint: str,
    model: str,
    redacted_key_id: str,
    schema_version: int,
    code_version: str,
) -> Dict[str, Any]:
    """Build a fresh cell skeleton in `pending` status.

    `schema_version` here is the *step adapter's* local schema (a cache
    invalidation lever; see orchestration.cache); it is intentionally
    independent of the run-record `SCHEMA_VERSION` constant above.
    """
    return {
        "step": step,
        "endpoint": endpoint,
        "model": model,
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "latency_ms": None,
        "cache_hit": False,
        "redacted_key_id": redacted_key_id,
        "schema_version": schema_version,
        "code_version": code_version,
        "payload": None,
        "error": None,
    }


def _recompute_summary(cells: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(cells)
    ok = sum(1 for c in cells if c.get("status") == "ok")
    error = sum(1 for c in cells if c.get("status") == "error")
    cache_hits = sum(1 for c in cells if c.get("cache_hit"))
    cache_misses = total - cache_hits
    hit_rate = (cache_hits / total) if total else 0.0
    return {
        "total": total,
        "ok": ok,
        "error": error,
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "hit_rate": hit_rate,
    }


class RunRecordWriter:
    """Serialise a run-record dict to disk after validating the v1 surface."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)

    def write(self, record: Dict[str, Any], *, elapsed_seconds: float) -> Path:
        if record.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(
                f"refusing to write: schema_version={record.get('schema_version')!r} "
                f"does not match v{SCHEMA_VERSION}; either bump SCHEMA_VERSION "
                f"deliberately or fix the producer"
            )

        unknown_top = set(record.keys()) - set(_TOPLEVEL_FIELDS)
        if unknown_top:
            raise ValueError(
                f"refusing to write: unknown top-level fields {sorted(unknown_top)} "
                f"appeared without a schema version bump"
            )

        for i, cell in enumerate(record["cells"]):
            unknown_cell = set(cell.keys()) - set(_CELL_FIELDS)
            if unknown_cell:
                raise ValueError(
                    f"refusing to write cell[{i}]: unknown cell fields "
                    f"{sorted(unknown_cell)} appeared without a schema version bump"
                )

        record["elapsed_seconds"] = float(elapsed_seconds)
        record["summary"] = _recompute_summary(record["cells"])

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return self._path


__all__ = [
    "SCHEMA_VERSION",
    "TOOL_NAME",
    "RunRecordWriter",
    "new_cell",
    "new_record",
]
