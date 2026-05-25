"""Tests for orchestration.runner — matrix walk + isolation + concurrency + cache."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from orchestration.cache import Cache
from orchestration.config import OrchestrationConfig, RunParams
from orchestration.run_record import RunRecordWriter
from orchestration.runner import run_matrix
from orchestration.steps import EndpointSpec, StepContext, StepResult
from security.bearer import CredentialBearer


def _ep(name: str, url: str = "https://x.test") -> EndpointSpec:
    return EndpointSpec(
        name=name, base_url=url,
        credential=CredentialBearer("sk-fake-NOT-A-REAL-KEY-1234567890" + name),
    )


def _cfg(*, steps=("probe",), concurrency=1, mock=True, endpoints=None,
         models=None) -> OrchestrationConfig:
    eps = endpoints or [_ep("ep-a")]
    params = {"models": models or {"default": ["m1"]}}
    return OrchestrationConfig(
        run=RunParams(steps=tuple(steps), concurrency=concurrency, mock=mock),
        endpoints=eps,
        params=params,
        config_digest="d" * 64,
    )


def _ok_adapter(payload_factory=None):
    def fn(ctx: StepContext) -> StepResult:
        return {
            "step": ctx.step, "endpoint": ctx.endpoint.name, "model": ctx.model,
            "status": "ok", "latency_ms": 1.0,
            "payload": (payload_factory(ctx) if payload_factory else {"ok": True}),
            "schema_version": 1, "code_version": "test",
        }
    fn.__name__ = "step_probe"
    return fn


def _raising_adapter(message: str = "boom"):
    def fn(ctx: StepContext) -> StepResult:
        raise RuntimeError(message)
    fn.__name__ = "step_probe"
    return fn


# ---- Matrix walk (AC-S5-003) ----------------------------------------------

def test_matrix_walks_step_x_endpoint_x_model():
    cfg = _cfg(
        steps=("probe", "perf"),
        endpoints=[_ep("ep-a"), _ep("ep-b")],
        models={"default": ["m1", "m2"]},
    )
    rec = run_matrix(cfg, adapters={"probe": _ok_adapter(), "perf": _ok_adapter()})
    # 2 steps x 2 endpoints x 2 models = 8 cells
    assert len(rec["cells"]) == 8
    seen = {(c["step"], c["endpoint"], c["model"]) for c in rec["cells"]}
    assert seen == {
        (s, e, m) for s in ("probe", "perf") for e in ("ep-a", "ep-b") for m in ("m1", "m2")
    }


def test_matrix_36_cell_dry_run_like_shape():
    # AC-S5-001 surrogate — 3 endpoints x 3 models x 4 steps = 36 cells
    eps = [_ep(f"ep-{i}") for i in range(3)]
    cfg = _cfg(
        steps=("probe", "purity", "perf", "pricing"),
        endpoints=eps,
        models={"default": ["m1", "m2", "m3"]},
    )
    adapter = _ok_adapter()
    rec = run_matrix(
        cfg, adapters={k: adapter for k in ("probe", "purity", "perf", "pricing")},
    )
    assert len(rec["cells"]) == 36
    assert rec["summary"]["total"] == 36
    assert rec["summary"]["ok"] == 36


# ---- Isolation (AC-S5-010) -------------------------------------------------

def test_single_cell_failure_does_not_abort_matrix():
    eps = [_ep("ep-a"), _ep("ep-b"), _ep("ep-c")]
    cfg = _cfg(endpoints=eps, models={"default": ["m"]})

    raised_for: list[str] = []

    def adapter(ctx: StepContext) -> StepResult:
        if ctx.endpoint.name == "ep-b":
            raised_for.append("ep-b")
            raise RuntimeError("simulated failure")
        return _ok_adapter()(ctx)
    adapter.__name__ = "step_probe"

    rec = run_matrix(cfg, adapters={"probe": adapter})
    assert len(rec["cells"]) == 3
    by_ep = {c["endpoint"]: c for c in rec["cells"]}
    assert by_ep["ep-a"]["status"] == "ok"
    assert by_ep["ep-c"]["status"] == "ok"
    assert by_ep["ep-b"]["status"] == "error"
    assert "simulated failure" in by_ep["ep-b"]["error"]["message"]
    assert rec["summary"]["error"] == 1
    assert rec["summary"]["ok"] == 2


def test_error_message_is_redacted():
    secret = "sk-LEAKING-THIS-SHOULD-NEVER-HAPPEN-LONGLONG"

    def adapter(ctx: StepContext) -> StepResult:
        raise RuntimeError(f"upstream rejected key {secret}")
    adapter.__name__ = "step_probe"

    cfg = _cfg()
    rec = run_matrix(cfg, adapters={"probe": adapter})
    cell = rec["cells"][0]
    assert cell["status"] == "error"
    # Secret must NOT appear anywhere in the error blob.
    import json
    blob = json.dumps(cell["error"])
    assert secret not in blob


# ---- Cache hit / miss (AC-S5-004 + AC-S5-005) ------------------------------

def test_second_run_hits_cache(tmp_path: Path):
    cfg = _cfg(endpoints=[_ep("ep-a")], models={"default": ["m1"]})
    cache = Cache(tmp_path)
    adapter = _ok_adapter()

    rec1 = run_matrix(cfg, cache=cache, adapters={"probe": adapter})
    assert rec1["cells"][0]["cache_hit"] is False

    rec2 = run_matrix(cfg, cache=cache, adapters={"probe": adapter})
    assert rec2["cells"][0]["cache_hit"] is True
    assert rec2["summary"]["cache_hits"] == 1
    assert rec2["summary"]["hit_rate"] == 1.0


def test_no_cache_when_cache_is_none():
    cfg = _cfg()
    rec = run_matrix(cfg, cache=None, adapters={"probe": _ok_adapter()})
    assert all(c["cache_hit"] is False for c in rec["cells"])


def test_disabled_cache_misses_every_lookup(tmp_path: Path):
    cfg = _cfg()
    cache = Cache(tmp_path, disabled=True)
    # First run...
    run_matrix(cfg, cache=cache, adapters={"probe": _ok_adapter()})
    # ...second run should still miss because cache is disabled.
    rec = run_matrix(cfg, cache=cache, adapters={"probe": _ok_adapter()})
    assert rec["cells"][0]["cache_hit"] is False


def test_failed_cells_are_not_cached(tmp_path: Path):
    cfg = _cfg()
    cache = Cache(tmp_path)
    run_matrix(cfg, cache=cache, adapters={"probe": _raising_adapter()})
    # Now switch to an OK adapter; the cell should still miss because the
    # previous error was not cached.
    rec = run_matrix(cfg, cache=cache, adapters={"probe": _ok_adapter()})
    assert rec["cells"][0]["cache_hit"] is False


# ---- Concurrency (AC-S5-009) -----------------------------------------------

def test_concurrency_actually_parallelises_step_cells():
    eps = [_ep(f"ep-{i}") for i in range(8)]
    cfg = _cfg(concurrency=4, endpoints=eps, models={"default": ["m"]})

    def slow_adapter(ctx: StepContext) -> StepResult:
        time.sleep(0.1)
        return _ok_adapter()(ctx)
    slow_adapter.__name__ = "step_probe"

    t0 = time.perf_counter()
    rec = run_matrix(cfg, adapters={"probe": slow_adapter})
    elapsed = time.perf_counter() - t0

    # Sequential lower bound = 8 * 0.1 = 0.8s. With concurrency=4, expect
    # roughly 0.2s (=2 batches of 4 in parallel). Generous upper bound to
    # absorb CI noise: must be < 0.6 (i.e. <0.6 * sequential, per AC-S5-009).
    assert len(rec["cells"]) == 8
    assert elapsed < 0.5, f"expected concurrency win; got elapsed={elapsed:.3f}s"


# ---- Run-record output ----------------------------------------------------

def test_runner_writes_run_record_via_writer(tmp_path: Path):
    cfg = _cfg()
    out = tmp_path / "run-record.json"
    writer = RunRecordWriter(out)
    rec = run_matrix(cfg, writer=writer, adapters={"probe": _ok_adapter()})
    assert out.exists()
    import json
    parsed = json.loads(out.read_text())
    assert parsed["schema_version"] == 1
    assert parsed["summary"]["total"] == 1
    assert parsed["tool"] == "audit-matrix"


def test_runner_records_redacted_key_ids():
    eps = [_ep("ep-a"), _ep("ep-b")]
    cfg = _cfg(endpoints=eps, models={"default": ["m"]})
    rec = run_matrix(cfg, adapters={"probe": _ok_adapter()})
    assert len(rec["redacted_key_ids"]) == 2
    # Each cell records the redacted id, never the plaintext.
    for c in rec["cells"]:
        assert len(c["redacted_key_id"]) == 8


def test_runner_skips_step_with_no_registered_adapter():
    cfg = _cfg(steps=("probe", "nonexistent_step"))
    # `nonexistent_step` isn't in DEFAULT_STEPS so we have to bypass config
    # validation -- build the RunParams directly.
    cfg.run = RunParams(steps=("probe", "ghost"), concurrency=1, mock=True)
    rec = run_matrix(cfg, adapters={"probe": _ok_adapter()})
    # Only `probe` cells produced.
    assert {c["step"] for c in rec["cells"]} == {"probe"}


# ---- Cache invalidation via code_version (AC-S5-005 lever 4) --------------

def test_code_version_bump_invalidates_cache(tmp_path: Path):
    import orchestration.steps as steps_mod
    cfg = _cfg()
    cache = Cache(tmp_path)

    run_matrix(cfg, cache=cache, adapters={"probe": _ok_adapter()})
    # Bump the probe code_version constant -> next run misses.
    original = steps_mod.PROBE_CODE_VERSION
    try:
        steps_mod.PROBE_CODE_VERSION = "bumped"
        rec = run_matrix(cfg, cache=cache, adapters={"probe": _ok_adapter()})
        assert rec["cells"][0]["cache_hit"] is False
    finally:
        steps_mod.PROBE_CODE_VERSION = original
