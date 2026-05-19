"""Benchmark runner: orchestrates streaming calls across endpoints / models.

Loading config, walking the test matrix, executing rounds (sequential or
concurrent), and producing the structured result dict consumed by
:mod:`api_relay_audit.perf.report`.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Optional

from api_relay_audit.perf.metrics import summarize_latencies
from api_relay_audit.perf.purity import (PurityRecord, analyze_purity,
                                         analyze_response)
from api_relay_audit.perf.streaming import (StreamingClient, StreamResult,
                                            fetch_models)


DEFAULT_PROMPT = "请介绍北京好吃的"
DEFAULT_ROUNDS = 10
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_TOKENS = 512
DEFAULT_TEMPERATURE: Optional[float] = None  # omit from request body by default
DEFAULT_CONCURRENCY = 1

# Vendor presets — a small set of representative newer-flagship models
# per vendor. Used when the user specifies a vendor (CLI ``--vendor`` or
# YAML ``vendor:``) instead of an explicit model list. Mixed-vendor keys
# can still set ``models:`` directly to override.
VENDOR_MODELS: dict[str, list[str]] = {
    "gpt": [
        "gpt-5.2",         # newer GPT-5 mid-tier
        "gpt-5.3-codex",   # code-tuned GPT-5
        "gpt-5.5",         # newest GPT-5 flagship
    ],
    "claude": [
        "claude-haiku-4-5",    # fast / cheap
        "claude-sonnet-4-6",   # mainstream Claude 4
        "claude-opus-4-7",     # flagship
    ],
}

# Backward-compatible "default" model set: union of the gpt + claude
# presets so a no-vendor invocation still exercises both vendors and
# surfaces 4xx errors for the ones a key cannot reach.
DEFAULT_MODELS: list[str] = VENDOR_MODELS["gpt"] + VENDOR_MODELS["claude"]


def vendor_models(vendor: str) -> list[str]:
    """Return the representative model list for ``vendor`` (case-insensitive)."""
    key = (vendor or "").strip().lower()
    if key not in VENDOR_MODELS:
        raise ValueError(
            f"unknown vendor {vendor!r}; choose from "
            f"{sorted(VENDOR_MODELS)}"
        )
    return list(VENDOR_MODELS[key])


def load_config(path: str) -> dict:
    """Load a YAML benchmark config. ``yaml`` import is optional.

    Returns a normalised config dict with the following keys::

        {
          "test": {prompt, rounds, timeout, max_tokens, temperature,
                   concurrency, format},
          "endpoints": [{name, base_url, api_key, models, format}, ...],
          "default_models": [str, ...],
        }
    """
    try:
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover
        raise SystemExit(
            "PyYAML is required for --config. Install with `pip install pyyaml`."
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping, got {type(raw).__name__}")

    test = raw.get("test") or {}
    raw_temp = test.get("temperature", DEFAULT_TEMPERATURE)
    temperature = None if raw_temp is None else float(raw_temp)
    cfg = {
        "test": {
            "prompt": test.get("prompt", DEFAULT_PROMPT),
            "rounds": int(test.get("rounds", DEFAULT_ROUNDS)),
            "timeout": float(test.get("timeout", DEFAULT_TIMEOUT)),
            "max_tokens": int(test.get("max_tokens", DEFAULT_MAX_TOKENS)),
            "temperature": temperature,
            "concurrency": int(test.get("concurrency", DEFAULT_CONCURRENCY)),
            "format": test.get("format", "openai"),
            "system": test.get("system"),
            "prompts": test.get("prompts"),
        },
        "default_models": list(raw.get("default_models", [])),
        "endpoints": [],
    }
    for ep in raw.get("endpoints", []):
        if not isinstance(ep, dict):
            continue
        if not ep.get("base_url") or not ep.get("api_key"):
            raise ValueError(f"endpoint missing base_url/api_key: {ep!r}")
        explicit_models = list(ep.get("models", []))
        vendor = ep.get("vendor")
        if not explicit_models and vendor:
            explicit_models = vendor_models(vendor)
        default_name = _short_host(ep["base_url"])
        if vendor:
            default_name = f"{default_name}-{vendor}"
        cfg["endpoints"].append({
            "name": ep.get("name") or default_name,
            "base_url": ep["base_url"],
            "api_key": ep["api_key"],
            "models": explicit_models or None,
            "format": ep.get("format", cfg["test"]["format"]),
            "vendor": vendor,
        })
    if not cfg["endpoints"]:
        raise ValueError("config must define at least one endpoint")
    return cfg


def _short_host(url: str) -> str:
    h = url.split("//", 1)[-1].split("/", 1)[0]
    return h.replace(":", "_")


# -- Core run loop -----------------------------------------------------------

def _execute_one(client: StreamingClient, *, model: str, prompt: str,
                 system: Optional[str], max_tokens: int,
                 temperature: float) -> StreamResult:
    return client.stream(model=model, prompt=prompt, system=system,
                         max_tokens=max_tokens, temperature=temperature)


def _run_rounds_for_model(*, endpoint: dict, model: str, test: dict,
                          log) -> dict:
    """Run ``rounds`` streamed calls for one (endpoint, model) pair."""
    rounds = test["rounds"]
    concurrency = max(1, test["concurrency"])
    prompts: list[str] = test.get("prompts") or [test["prompt"]]
    timeout = test["timeout"]
    max_tokens = test["max_tokens"]
    temperature = test["temperature"]
    system = test.get("system")

    client = StreamingClient(endpoint["base_url"], endpoint["api_key"],
                             timeout=timeout, format=endpoint["format"])

    results: list[StreamResult] = [None] * rounds  # type: ignore

    def _run(idx: int) -> None:
        prompt = prompts[idx % len(prompts)]
        log(f"    round {idx + 1}/{rounds} prompt={prompt[:24]!r}")
        results[idx] = _execute_one(
            client, model=model, prompt=prompt, system=system,
            max_tokens=max_tokens, temperature=temperature)

    if concurrency == 1:
        for i in range(rounds):
            _run(i)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as ex:
            list(ex.map(_run, range(rounds)))

    # Convert SSE results -> JSON-serialisable rows + purity records
    rows: list[dict] = []
    purity_records: list[PurityRecord] = []
    ttft_values: list[float] = []
    total_values: list[float] = []
    output_chars: list[int] = []

    for idx, r in enumerate(results):
        prompt = prompts[idx % len(prompts)]
        rec = analyze_response(r.text, prompt=prompt, model=model,
                               ok=r.ok, round_index=idx + 1)
        purity_records.append(rec)
        if r.ok and r.ttft is not None:
            ttft_values.append(r.ttft)
        if r.ok:
            total_values.append(r.total_time)
        if r.text:
            output_chars.append(len(r.text))
        rows.append({
            "round": idx + 1,
            "ok": r.ok,
            "status_code": r.status_code,
            "ttft_seconds": r.ttft,
            "total_seconds": r.total_time,
            "output_chars": len(r.text or ""),
            "chunk_count": r.chunk_count,
            "finish_reason": r.finish_reason,
            "error": r.error,
            "format": r.format,
            "text_preview": (r.text or "")[:280],
            "issues": list(rec.issues),
            "language_mismatch": rec.language_mismatch,
            "identities_detected": list(rec.identities),
        })

    purity = analyze_purity(purity_records)

    return {
        "model": model,
        "rounds": rows,
        "errors": [r["error"] for r in rows if not r["ok"]],
        "metrics": {
            "ttft_seconds": summarize_latencies(ttft_values),
            "total_seconds": summarize_latencies(total_values),
            "output_chars": summarize_latencies(output_chars),
            "success_rate": (sum(1 for r in rows if r["ok"]) / len(rows)
                             if rows else 0.0),
            "successful_rounds": sum(1 for r in rows if r["ok"]),
            "failed_rounds": sum(1 for r in rows if not r["ok"]),
        },
        "purity": purity.as_dict(),
    }


# -- Public driver -----------------------------------------------------------

def run_benchmark(config: dict, *, model_filter: Optional[list[str]] = None,
                  log_fn=None) -> dict:
    """Run the full benchmark matrix and return a JSON-serialisable result."""
    log = log_fn or (lambda msg: print(msg, file=sys.stderr))
    started = time.perf_counter()
    started_iso = datetime.now(timezone.utc).isoformat()

    test = config["test"]
    default_models = config.get("default_models") or DEFAULT_MODELS

    endpoint_results = []
    for ep in config["endpoints"]:
        log(f"[endpoint] {ep['name']} -> {ep['base_url']}")
        # Try /v1/models for visibility
        listed_models, list_err = fetch_models(ep["base_url"], ep["api_key"])
        if list_err:
            log(f"  /v1/models failed: {list_err}")
        else:
            log(f"  /v1/models returned {len(listed_models)} ids")

        models = ep["models"] or default_models
        if model_filter:
            models = [m for m in models if m in set(model_filter)]
        log(f"  testing models: {models}")

        per_model = []
        for model in models:
            log(f"  [model] {model}")
            try:
                row = _run_rounds_for_model(
                    endpoint=ep, model=model, test=test, log=log)
            except Exception as e:  # pragma: no cover — defensive
                log(f"    fatal error during model {model}: {e}")
                row = {
                    "model": model,
                    "rounds": [],
                    "errors": [f"{type(e).__name__}: {e}"],
                    "metrics": None,
                    "purity": None,
                    "fatal": True,
                }
            per_model.append(row)

        endpoint_results.append({
            "name": ep["name"],
            "base_url": ep["base_url"],
            "format": ep["format"],
            "vendor": ep.get("vendor"),
            "models_tested": models,
            "models_listed": listed_models,
            "models_listed_error": list_err,
            "results": per_model,
        })

    elapsed = time.perf_counter() - started
    return {
        "schema_version": 1,
        "tool": "api-relay-audit perf-bench",
        "generated_at": started_iso,
        "elapsed_seconds": elapsed,
        "test": {**test, "default_models": default_models},
        "endpoints": endpoint_results,
    }
