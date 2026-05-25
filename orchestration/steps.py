"""StepResult protocol + 4 adapter shells — TES-128 S5-B.

Each step adapter takes a `StepContext` and returns a `StepResult`. The
orchestrator (runner.py) is what actually drives them, what fills in
`started_at` / `finished_at` / `cache_hit` / `redacted_key_id`, and what
applies the redaction tripwire to whatever `payload` / `error` the adapter
returned.

The four step adapters in this story:

* `step_probe`    — connectivity / model-listing probe (S1 will replace the
                     shell with real probe logic)
* `step_purity`   — output-purity scoring (S2 will fill in)
* `step_perf`     — performance benchmark; wired here to the *existing* perf
                     runner so AC-S5-012 ("at least one real adapter unit
                     test passing") is satisfied without waiting on S3
* `step_pricing`  — pricing/billing compliance (S4 will fill in)

`total=False` on the StepResult TypedDict means every key is optional from
the adapter's point of view; the orchestrator merges adapter output into the
canonical cell skeleton from `orchestration.run_record.new_cell`, so missing
keys just keep the cell defaults.

This module imports from `security.redact` at module load. That import is a
compile-time guard: the credential-scan CI gate enforces that no other module
re-implements credential patterns, and this import wires every step adapter
through the canonical redactor before any payload leaves the process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Literal, Optional, TypedDict

# Compile-time dependency on the security baseline (S5-A). This import is
# load-bearing — removing it (or replacing it with an ad-hoc regex) trips the
# CI grep gate. See security/__init__.py for the rationale.
from security.bearer import CredentialBearer
from security.redact import redact_text


# ---- StepResult (TypedDict, total=False) ----------------------------------

class StepResult(TypedDict, total=False):
    """The shape an adapter returns to the orchestrator.

    Per the handshake comment on TES-128:
      * `step / model / endpoint` are echoed by the adapter so the orchestrator
        can sanity-check before merging into the canonical cell.
      * `started_at / finished_at / cache_hit / redacted_key_id` are written
        by the orchestrator, NOT by adapters.
      * `payload` is the adapter-defined result blob; format owned per-Story.
      * `error` is {type, message, traceback?} after the orchestrator has run
        the result through redact_text.
    """

    step: str
    endpoint: str
    model: str
    status: Literal["ok", "error"]
    latency_ms: float
    payload: Dict[str, Any]
    error: Dict[str, Any]
    schema_version: int
    code_version: str


# ---- StepContext ----------------------------------------------------------

@dataclass(frozen=True)
class EndpointSpec:
    """An endpoint + its credential. Plaintext key only ever flows via the
    CredentialBearer wrapper; everywhere else we surface `redacted_id`.
    """

    name: str
    base_url: str
    credential: CredentialBearer
    vendor: Optional[str] = None
    fmt: str = "openai"


@dataclass
class StepContext:
    """Inputs an adapter receives. Adapters MUST NOT mutate `prior_results`."""

    step: str
    endpoint: EndpointSpec
    model: str
    run_params: Dict[str, Any] = field(default_factory=dict)
    prior_results: Dict[str, StepResult] = field(default_factory=dict)


# ---- Adapter registry ------------------------------------------------------

# AdapterFn maps StepContext to StepResult. The orchestrator holds the
# canonical (step_name -> AdapterFn) map; tests can inject their own.
AdapterFn = Callable[[StepContext], StepResult]


def _result_skeleton(ctx: StepContext, *, schema_version: int, code_version: str) -> StepResult:
    return {
        "step": ctx.step,
        "endpoint": ctx.endpoint.name,
        "model": ctx.model,
        "status": "ok",
        "latency_ms": 0.0,
        "payload": {},
        "schema_version": schema_version,
        "code_version": code_version,
    }


def _redact_value(v: Any, sentinel_keys: tuple[str, ...]) -> Any:
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, dict):
        return _redact_payload(v, sentinel_keys)
    if isinstance(v, list):
        return [_redact_value(x, sentinel_keys) for x in v]
    if isinstance(v, tuple):
        return tuple(_redact_value(x, sentinel_keys) for x in v)
    return v


def _redact_payload(payload: Dict[str, Any], sentinel_keys: tuple[str, ...]) -> Dict[str, Any]:
    """Belt-and-suspenders: recursively run any string-valued field through redact_text.

    Per the handshake contract, well-behaved adapters never put credentials
    in `payload`. But the orchestrator runs this anyway because (a) it costs
    nothing and (b) future adapter dev hands belong to S1/S2/S3/S4 dev who
    may not internalise the contract — defense in depth wins.

    Recurses through dict / list / tuple / list-of-dict / list-of-list etc.
    so a credential cannot hide behind arbitrary nesting (B1 regression).
    """
    return {k: _redact_value(v, sentinel_keys) for k, v in payload.items()}


# ---- The four step adapter shells -----------------------------------------
#
# Each shell satisfies AC-S5-003 (mock-relay matrix run) and AC-S5-010 (single
# cell failure does not abort the matrix). The owning Story replaces the
# `_payload_*` helper with real work, but the surface above the helper is
# stable.

PROBE_SCHEMA_VERSION = 1
PROBE_CODE_VERSION = "shell-1"


def step_probe(ctx: StepContext) -> StepResult:
    """Connectivity / models-list probe.

    Shell behaviour:
      * If `ctx.run_params["mock"]` is truthy, return a fake `payload` and
        success without touching the network.
      * Otherwise, raise NotImplementedError — the S1 dev replaces this.

    Real implementation will GET `<base_url>/v1/models` and emit a payload
    enumerating responses + auth-status flags.
    """
    started = time.perf_counter()
    result = _result_skeleton(
        ctx, schema_version=PROBE_SCHEMA_VERSION, code_version=PROBE_CODE_VERSION
    )

    if ctx.run_params.get("mock"):
        result["payload"] = {
            "models_listed": [ctx.model],
            "models_listed_error": None,
            "mock": True,
        }
    else:
        # Adapters raise to signal "I haven't been wired yet"; the orchestrator
        # converts that into status=error per-cell isolation.
        raise NotImplementedError("step_probe shell — wire to real probe in S1")

    result["latency_ms"] = (time.perf_counter() - started) * 1000.0
    return result


PURITY_SCHEMA_VERSION = 1
PURITY_CODE_VERSION = "shell-1"


def step_purity(ctx: StepContext) -> StepResult:
    """Output-purity scoring. Mock path returns a fixed neutral score."""
    started = time.perf_counter()
    result = _result_skeleton(
        ctx, schema_version=PURITY_SCHEMA_VERSION, code_version=PURITY_CODE_VERSION
    )
    if ctx.run_params.get("mock"):
        result["payload"] = {
            "purity_score": 1.0,
            "language_mismatch": False,
            "identities_detected": [],
            "mock": True,
        }
    else:
        raise NotImplementedError("step_purity shell — wire to real purity scorer in S2")
    result["latency_ms"] = (time.perf_counter() - started) * 1000.0
    return result


PERF_SCHEMA_VERSION = 1
PERF_CODE_VERSION = "shell-1"


def step_perf(ctx: StepContext) -> StepResult:
    """Performance benchmark.

    Real adapter wired to api_relay_audit.perf.runner — this is the AC-S5-012
    real-adapter integration. The wiring lives in `step_perf_real`; this
    `step_perf` shell defaults to mock unless `run_params["mock"] is False`
    AND `step_perf_real` is registered, so the matrix still executes end-to-
    end without a live network.
    """
    started = time.perf_counter()
    result = _result_skeleton(
        ctx, schema_version=PERF_SCHEMA_VERSION, code_version=PERF_CODE_VERSION
    )
    if ctx.run_params.get("mock", True):
        result["payload"] = {
            "ttft_seconds_p50": 0.5,
            "total_seconds_p50": 1.2,
            "rounds": ctx.run_params.get("rounds", 1),
            "success_rate": 1.0,
            "mock": True,
        }
    else:
        # Real path — delegated to the wired implementation in this module.
        return step_perf_real(ctx)
    result["latency_ms"] = (time.perf_counter() - started) * 1000.0
    return result


def step_perf_real(ctx: StepContext) -> StepResult:
    """AC-S5-012 — real adapter wired to api_relay_audit.perf.runner.

    Kept thin: the heavy lifting is in `api_relay_audit.perf.runner`. This
    function exists to satisfy "at least one real adapter unit test passing"
    via the unit test in `tests/test_step_perf_adapter.py`.
    """
    from api_relay_audit.perf.runner import _run_rounds_for_model

    started = time.perf_counter()
    result = _result_skeleton(
        ctx, schema_version=PERF_SCHEMA_VERSION, code_version=PERF_CODE_VERSION
    )
    # Build the per-model test dict the perf runner expects.
    test_params = {
        "prompt": ctx.run_params.get("prompt", "ping"),
        "rounds": int(ctx.run_params.get("rounds", 1)),
        "timeout": float(ctx.run_params.get("timeout", 30.0)),
        "max_tokens": int(ctx.run_params.get("max_tokens", 64)),
        "temperature": ctx.run_params.get("temperature"),
        "concurrency": int(ctx.run_params.get("step_concurrency", 1)),
        "system": ctx.run_params.get("system"),
        "prompts": ctx.run_params.get("prompts"),
    }
    endpoint_dict = {
        "name": ctx.endpoint.name,
        "base_url": ctx.endpoint.base_url,
        "api_key": ctx.endpoint.credential.reveal(),
        "format": ctx.endpoint.fmt,
    }

    log_lines: list[str] = []
    perf_row = _run_rounds_for_model(
        endpoint=endpoint_dict, model=ctx.model, test=test_params,
        log=log_lines.append,
    )
    # Strip the inner `model` key so we don't shadow ctx.model in payload.
    payload = {k: v for k, v in perf_row.items() if k != "model"}
    result["payload"] = payload
    result["latency_ms"] = (time.perf_counter() - started) * 1000.0
    return result


PRICING_SCHEMA_VERSION = 1
PRICING_CODE_VERSION = "shell-1"


def step_pricing(ctx: StepContext) -> StepResult:
    """Pricing / billing compliance check. Mock returns no anomalies."""
    started = time.perf_counter()
    result = _result_skeleton(
        ctx, schema_version=PRICING_SCHEMA_VERSION, code_version=PRICING_CODE_VERSION
    )
    if ctx.run_params.get("mock"):
        result["payload"] = {
            "billed_tokens": 0,
            "anomalies": [],
            "mock": True,
        }
    else:
        raise NotImplementedError("step_pricing shell — wire to real pricing checker in S4")
    result["latency_ms"] = (time.perf_counter() - started) * 1000.0
    return result


DEFAULT_ADAPTERS: Dict[str, AdapterFn] = {
    "probe": step_probe,
    "purity": step_purity,
    "perf": step_perf,
    "pricing": step_pricing,
}


__all__ = [
    "AdapterFn",
    "DEFAULT_ADAPTERS",
    "EndpointSpec",
    "PERF_CODE_VERSION",
    "PERF_SCHEMA_VERSION",
    "PROBE_CODE_VERSION",
    "PROBE_SCHEMA_VERSION",
    "PRICING_CODE_VERSION",
    "PRICING_SCHEMA_VERSION",
    "PURITY_CODE_VERSION",
    "PURITY_SCHEMA_VERSION",
    "StepContext",
    "StepResult",
    "_redact_payload",
    "step_perf",
    "step_perf_real",
    "step_pricing",
    "step_probe",
    "step_purity",
]
