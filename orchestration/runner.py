"""Matrix runner — walks (step x endpoint x model), with isolation + concurrency.

Walks the matrix:

    for step in run.steps:
        for endpoint in cfg.endpoints:
            for model in endpoint.models:
                drive_one(step, endpoint, model)   # produces 1 cell

Isolation contract (AC-S5-010): when any single cell raises, the cell is
recorded with `status="error"` + a redacted `error` blob, and the matrix
walk continues. Nothing aborts the run except an OS-level signal.

Concurrency contract (AC-S5-009): cells inside a step are driven via a
ThreadPoolExecutor with `max_workers = cfg.run.concurrency` (default 4).
Steps run sequentially, because S5-C may consume prior step results via
`StepContext.prior_results` -- keeping a global thread-pool would require
synchronisation we'd rather not own at this layer.

Cache contract:
  * Before invoking the adapter, the runner computes the cache key and tries
    `cache.get`. On hit, the cell is marked `cache_hit=True` and the payload
    is taken from cache; the adapter is NOT called.
  * On miss + successful adapter call, the result is written to cache.
  * On adapter raise, the cache is NOT written.

Credential contract:
  * The adapter receives the full `CredentialBearer` via `StepContext`.
  * The cell records `redacted_key_id` (the bearer's `redacted_id`).
  * `payload` / `error` are passed through `_redact_payload` and `redact_text`
    before being written to the cell. This is belt-and-suspenders -- the
    handshake contract says adapters should not include credentials, but we
    do not trust adapter code to honour that contract.
"""

from __future__ import annotations

import concurrent.futures as cf
import time
import traceback
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Mapping, Optional

from orchestration.cache import Cache, compute_cache_key
from orchestration.config import OrchestrationConfig
from orchestration.run_record import (
    RunRecordWriter,
    new_cell,
    new_record,
)
from orchestration.steps import (
    DEFAULT_ADAPTERS,
    AdapterFn,
    EndpointSpec,
    StepContext,
    StepResult,
    _redact_payload,
)
from security.redact import redact_text


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_models(ep: EndpointSpec, params: Mapping[str, Any]) -> List[str]:
    """Pick the model list for an endpoint."""
    by_endpoint = (params.get("models") or {}).get(ep.name)
    if by_endpoint:
        return list(by_endpoint)
    default = (params.get("models") or {}).get("default")
    if default:
        return list(default)
    return ["default"]


def _step_adapter_versions(adapter: AdapterFn) -> tuple[int, str]:
    """Read the (schema_version, code_version) for a step adapter."""
    import orchestration.steps as steps_mod

    name = getattr(adapter, "__name__", "").removeprefix("step_").upper()
    schema = getattr(steps_mod, f"{name}_SCHEMA_VERSION", 1)
    code = getattr(steps_mod, f"{name}_CODE_VERSION", "unknown")
    return int(schema), str(code)


def _build_cell_params(
    run_params: Mapping[str, Any], step_params: Mapping[str, Any],
) -> Dict[str, Any]:
    """The subset of params that influences this step's output (and key)."""
    out: Dict[str, Any] = {}
    out.update(step_params or {})
    out["mock"] = bool(run_params.get("mock", False))
    return out


def _run_one_cell(
    *,
    step: str,
    endpoint: EndpointSpec,
    model: str,
    cfg: OrchestrationConfig,
    adapter: AdapterFn,
    cache: Optional[Cache],
    prior_results: Dict[str, StepResult],
) -> Dict[str, Any]:
    """Drive one (step, endpoint, model) cell. Never raises."""
    schema_v, code_v = _step_adapter_versions(adapter)
    cell = new_cell(
        step=step, endpoint=endpoint.name, model=model,
        redacted_key_id=endpoint.credential.redacted_id,
        schema_version=schema_v, code_version=code_v,
    )
    cell["started_at"] = _utc_now_iso()
    started = time.perf_counter()

    step_params = (cfg.params or {}).get(step, {})
    cell_params = _build_cell_params(vars(cfg.run), step_params)
    key = compute_cache_key(
        step=step, base_url=endpoint.base_url, model=model,
        schema_version=schema_v, code_version=code_v, params=cell_params,
    )

    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            cell["cache_hit"] = True
            cell["status"] = "ok"
            cell["payload"] = _redact_payload(cached, sentinel_keys=())
            cell["latency_ms"] = (time.perf_counter() - started) * 1000.0
            cell["finished_at"] = _utc_now_iso()
            return cell

    ctx = StepContext(
        step=step,
        endpoint=endpoint,
        model=model,
        run_params={**(step_params or {}), **vars(cfg.run)},
        prior_results=prior_results,
    )
    try:
        adapter_result: StepResult = adapter(ctx)
        cell["status"] = adapter_result.get("status", "ok")
        cell["payload"] = _redact_payload(
            dict(adapter_result.get("payload") or {}), sentinel_keys=(),
        )
        cell["latency_ms"] = float(
            adapter_result.get("latency_ms", (time.perf_counter() - started) * 1000.0)
        )
        if cache is not None and cell["status"] == "ok":
            cache.set(key, cell["payload"], step=step)
    except Exception as exc:
        cell["status"] = "error"
        cell["error"] = {
            "type": type(exc).__name__,
            "message": redact_text(str(exc)),
            "traceback": redact_text("".join(traceback.format_exception(exc))),
        }
        cell["latency_ms"] = (time.perf_counter() - started) * 1000.0
    finally:
        cell["finished_at"] = _utc_now_iso()
    return cell


def run_matrix(
    cfg: OrchestrationConfig,
    *,
    cache: Optional[Cache] = None,
    adapters: Optional[Mapping[str, AdapterFn]] = None,
    code_version: str = "dev",
    writer: Optional[RunRecordWriter] = None,
    log_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Drive the matrix and return the run-record dict."""
    log = log_fn or (lambda _msg: None)
    started = time.perf_counter()

    redacted_ids = sorted({ep.credential.redacted_id for ep in cfg.endpoints})
    record = new_record(
        code_version=code_version,
        config_digest=cfg.config_digest,
        redacted_key_ids=redacted_ids,
    )

    adapter_map = dict(adapters or DEFAULT_ADAPTERS)
    prior_results: Dict[str, StepResult] = {}

    for step in cfg.run.steps:
        adapter = adapter_map.get(step)
        if adapter is None:
            log(f"[{step}] no adapter registered; skipping")
            continue

        descriptors: List[tuple[EndpointSpec, str]] = []
        for ep in cfg.endpoints:
            for model in _resolve_models(ep, cfg.params):
                descriptors.append((ep, model))

        log(f"[{step}] running {len(descriptors)} cells, concurrency={cfg.run.concurrency}")

        def _exec(pair: tuple[EndpointSpec, str]) -> Dict[str, Any]:
            ep, m = pair
            return _run_one_cell(
                step=step, endpoint=ep, model=m, cfg=cfg, adapter=adapter,
                cache=cache, prior_results=prior_results,
            )

        if cfg.run.concurrency <= 1 or len(descriptors) <= 1:
            cells = [_exec(d) for d in descriptors]
        else:
            with cf.ThreadPoolExecutor(max_workers=cfg.run.concurrency) as ex:
                cells = list(ex.map(_exec, descriptors))

        for c in cells:
            record["cells"].append(c)

    elapsed = time.perf_counter() - started

    if writer is not None:
        writer.write(record, elapsed_seconds=elapsed)
    else:
        record["elapsed_seconds"] = elapsed
        from orchestration.run_record import _recompute_summary
        record["summary"] = _recompute_summary(record["cells"])

    return record


__all__ = ["run_matrix"]
