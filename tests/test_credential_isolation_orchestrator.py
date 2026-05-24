"""Cross-orchestrator credential isolation regression — TES-128 S5-B DoD.

Companion to `tests/test_credential_isolation.py` (S5-A baseline). Exercises
the full audit-matrix path: load_config -> run_matrix -> RunRecordWriter,
and asserts that the run-record.json on disk + the in-memory result + any
captured stderr contain ZERO occurrences of a sentinel credential value.

Adapter behaviours under fuzz:
  * Adapter that successfully runs and returns a benign payload — sentinel
    must not appear because the orchestrator only writes redacted_key_id.
  * Adapter that raises an exception whose message *contains* the sentinel —
    sentinel must be redacted before reaching the cell error blob.
  * Adapter that misbehaves and stuffs the sentinel into its payload — the
    runner's `_redact_payload` defense must scrub it.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from orchestration.cache import Cache
from orchestration.config import OrchestrationConfig, RunParams
from orchestration.run_record import RunRecordWriter
from orchestration.runner import run_matrix
from orchestration.steps import EndpointSpec, StepContext, StepResult
from security.bearer import CredentialBearer


# Sentinel must look "key-shaped" enough that the redact patterns trip on it.
SENTINEL = "sk-SENTINEL-CROSS-ORCHESTRATOR-DO-NOT-LEAK-1234567890"


def _cfg(*, mock=True) -> OrchestrationConfig:
    return OrchestrationConfig(
        run=RunParams(steps=("probe",), concurrency=1, mock=mock),
        endpoints=[
            EndpointSpec(
                name="ep-sentinel",
                base_url="https://example.test",
                credential=CredentialBearer(SENTINEL),
            ),
        ],
        params={"models": {"default": ["m1"]}},
        config_digest="d" * 64,
    )


def _adapter_factory(mode: str):
    def fn(ctx: StepContext) -> StepResult:
        if mode == "raise_with_sentinel":
            raise RuntimeError(f"upstream barfed on {SENTINEL}")
        if mode == "leak_into_payload":
            return {
                "step": ctx.step, "endpoint": ctx.endpoint.name, "model": ctx.model,
                "status": "ok", "latency_ms": 1.0,
                "payload": {
                    "echoed_back": f"observed key {SENTINEL}",
                    "nested": {"deeper": [SENTINEL]},
                    "url": f"https://example.test/?api_key={SENTINEL}",
                },
                "schema_version": 1, "code_version": "test",
            }
        return {
            "step": ctx.step, "endpoint": ctx.endpoint.name, "model": ctx.model,
            "status": "ok", "latency_ms": 1.0,
            "payload": {"benign": True},
            "schema_version": 1, "code_version": "test",
        }
    fn.__name__ = "step_probe"
    return fn


def _assert_sentinel_absent(blob: str, *, ctx: str) -> None:
    assert SENTINEL not in blob, f"sentinel leaked in {ctx}"


# ---- happy path -----------------------------------------------------------

def test_benign_run_does_not_leak_sentinel(tmp_path: Path):
    out = tmp_path / "run-record.json"
    rec = run_matrix(
        _cfg(),
        adapters={"probe": _adapter_factory("benign")},
        writer=RunRecordWriter(out),
    )
    _assert_sentinel_absent(json.dumps(rec), ctx="in-memory record")
    _assert_sentinel_absent(out.read_text(encoding="utf-8"), ctx="run-record.json")
    # And the redacted_key_id is the right shape (8 hex chars).
    assert len(rec["cells"][0]["redacted_key_id"]) == 8


# ---- adapter raises with sentinel in the message ---------------------------

def test_raising_adapter_message_is_redacted(tmp_path: Path):
    out = tmp_path / "run-record.json"
    rec = run_matrix(
        _cfg(),
        adapters={"probe": _adapter_factory("raise_with_sentinel")},
        writer=RunRecordWriter(out),
    )
    cell = rec["cells"][0]
    assert cell["status"] == "error"
    _assert_sentinel_absent(json.dumps(cell["error"]), ctx="error blob")
    _assert_sentinel_absent(out.read_text(encoding="utf-8"), ctx="run-record.json")


# ---- adapter misbehaves and stuffs sentinel into payload ------------------

def test_payload_redaction_scrubs_misbehaving_adapter(tmp_path: Path):
    out = tmp_path / "run-record.json"
    rec = run_matrix(
        _cfg(),
        adapters={"probe": _adapter_factory("leak_into_payload")},
        writer=RunRecordWriter(out),
    )
    cell = rec["cells"][0]
    _assert_sentinel_absent(json.dumps(cell["payload"]), ctx="payload (in-memory)")
    _assert_sentinel_absent(out.read_text(encoding="utf-8"), ctx="run-record.json (disk)")


# ---- cache also gets the redacted version, not raw payload ----------------

def test_cache_does_not_persist_sentinel(tmp_path: Path):
    cache_dir = tmp_path / "cache"
    cache = Cache(cache_dir)
    run_matrix(
        _cfg(),
        adapters={"probe": _adapter_factory("leak_into_payload")},
        cache=cache,
    )
    for entry in cache_dir.glob("*.json"):
        _assert_sentinel_absent(entry.read_text(encoding="utf-8"),
                                ctx=f"cache file {entry.name}")


# ---- repr/str/format on CredentialBearer never leaks (S5-A invariant) ----

def test_credential_bearer_repr_never_leaks_sentinel():
    b = CredentialBearer(SENTINEL)
    _assert_sentinel_absent(repr(b), ctx="repr")
    _assert_sentinel_absent(str(b), ctx="str")
    _assert_sentinel_absent(f"{b!r}", ctx="f-string")
    _assert_sentinel_absent(f"{b}", ctx="f-string non-r")


# ---- logging propagation: even if the runner logs the cell, no leak ------

def test_logging_run_record_does_not_leak(caplog, tmp_path):
    """If anyone ever logs the cell at DEBUG level, the sentinel still must not leak."""
    out = tmp_path / "rr.json"
    with caplog.at_level(logging.DEBUG):
        rec = run_matrix(
            _cfg(),
            adapters={"probe": _adapter_factory("leak_into_payload")},
            writer=RunRecordWriter(out),
        )
        # Force a hypothetical downstream log of the in-memory cell.
        logging.getLogger("audit-matrix").debug("cell=%r", rec["cells"][0])
    for record in caplog.records:
        _assert_sentinel_absent(record.getMessage(), ctx=f"log record {record.name}")
