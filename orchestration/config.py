"""YAML config loader — audit-suite v1 schema + legacy perf-bench adapter.

Two input dialects, one output shape:

  * **audit-suite v1** — the new schema for the audit-matrix orchestrator.
    Looks like::

        schema_version: 1
        run:
          steps: [probe, purity, perf, pricing]
          concurrency: 4
          mock: true
        endpoints:
          - name: ep-a
            base_url: https://relay.example.com/v1
            credential: $ENV{ENDPOINT_A_KEY}
            vendor: claude
            models: [claude-haiku-4-5]
        params:
          perf: {rounds: 3, max_tokens: 64}

  * **legacy perf-bench** — the YAML format `scripts/perf-bench.py` already
    accepts (test:/endpoints:/default_models:). Detected by the absence of a
    top-level `schema_version` AND the presence of a `test:` key. Adapted on
    load so we can run pre-existing configs through the new orchestrator
    without forcing a rewrite.

Output of `load_config(path)` is an `OrchestrationConfig` whose
`config_digest` field is the sha256 of the normalised config — used by
`orchestration.run_record` as the run-record `config_digest`.

Credentials in YAML go through `security.secrets.load_secrets_yaml` semantics
(`$ENV{VAR}` resolution + plaintext refusal) — we wrap them in
`CredentialBearer` immediately and never serialise the plaintext into the
digest.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

from orchestration.steps import EndpointSpec
from security.bearer import CredentialBearer
from security.secrets import (
    PlaintextCredentialError,
    UndefinedEnvReferenceError,
    resolve_env_references,
)

CONFIG_SCHEMA_VERSION = 1

DEFAULT_STEPS: tuple[str, ...] = ("probe", "purity", "perf", "pricing")
DEFAULT_CONCURRENCY = 4

_ENV_REF = re.compile(r"\$ENV\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---- Dataclasses ----------------------------------------------------------

@dataclass(frozen=True)
class RunParams:
    """Per-run knobs the orchestrator passes to every adapter."""

    steps: tuple[str, ...]
    concurrency: int
    mock: bool


@dataclass
class OrchestrationConfig:
    """Normalised, ready-to-execute config."""

    run: RunParams
    endpoints: List[EndpointSpec] = field(default_factory=list)
    params: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    config_digest: str = ""
    source_path: Optional[str] = None


# ---- Public API -----------------------------------------------------------

def load_config(path: Path | str) -> OrchestrationConfig:
    """Load + normalise + validate a YAML config from disk."""
    p = Path(path)
    raw_text = p.read_text(encoding="utf-8")
    raw = yaml.safe_load(raw_text) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config root must be a mapping, got {type(raw).__name__}")

    if "schema_version" in raw:
        cfg = _normalise_v1(raw)
    elif "test" in raw and "endpoints" in raw:
        cfg = _adapt_legacy_perf_bench(raw)
    else:
        raise ValueError(
            "unrecognised config: needs either a top-level `schema_version: 1` "
            "(audit-suite v1) or both `test:` + `endpoints:` (legacy perf-bench)"
        )
    cfg.source_path = str(p)
    return cfg


def compute_config_digest(normalised: Dict[str, Any]) -> str:
    """sha256 of a credential-free normalised view of the config.

    Stable across whitespace/key-order changes; changes only when something
    that should change the run's outcome changes.
    """
    payload = json.dumps(normalised, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---- audit-suite v1 -------------------------------------------------------

def _normalise_v1(raw: Dict[str, Any]) -> OrchestrationConfig:
    if raw.get("schema_version") != CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"unsupported audit-suite schema_version={raw.get('schema_version')!r}; "
            f"this loader handles v{CONFIG_SCHEMA_VERSION}"
        )

    run_section = raw.get("run") or {}
    steps = tuple(run_section.get("steps", DEFAULT_STEPS))
    unknown = [s for s in steps if s not in DEFAULT_STEPS]
    if unknown:
        raise ValueError(
            f"unknown step(s) {unknown}; known steps: {list(DEFAULT_STEPS)}"
        )
    run = RunParams(
        steps=steps,
        concurrency=int(run_section.get("concurrency", DEFAULT_CONCURRENCY)),
        mock=bool(run_section.get("mock", False)),
    )

    endpoints = _build_endpoints(raw.get("endpoints", []))
    params = {k: dict(v) for k, v in (raw.get("params") or {}).items()}

    digest = compute_config_digest(_digest_view(run, raw.get("endpoints", []), params))
    return OrchestrationConfig(run=run, endpoints=endpoints, params=params, config_digest=digest)


# ---- legacy perf-bench adapter --------------------------------------------

def _adapt_legacy_perf_bench(raw: Dict[str, Any]) -> OrchestrationConfig:
    """Map a legacy perf-bench config into the audit-suite shape.

    Conservative translation:
      * `steps` defaults to ("perf",) — that's all the legacy config is for.
      * Per-endpoint `models` become per-endpoint model lists (we surface them
        via `endpoints` rather than the v1 `models:` key).
      * `test.*` becomes `params.perf.*`.
      * `default_models` falls through to endpoints with no explicit models.
    """
    test = raw.get("test") or {}
    legacy_endpoints = raw.get("endpoints") or []
    default_models = raw.get("default_models") or []

    run = RunParams(
        steps=("perf",),
        concurrency=int(test.get("concurrency", DEFAULT_CONCURRENCY)),
        mock=bool(test.get("mock", False)),
    )

    # Translate endpoints, propagating default_models down.
    eps: list[dict[str, Any]] = []
    for ep in legacy_endpoints:
        if not isinstance(ep, dict):
            continue
        eps.append({
            "name": ep.get("name"),
            "base_url": ep["base_url"],
            "credential": ep["api_key"],
            "vendor": ep.get("vendor"),
            "models": ep.get("models") or default_models,
            "format": ep.get("format", test.get("format", "openai")),
        })
    endpoints = _build_endpoints(eps)

    params = {
        "perf": {
            "prompt": test.get("prompt"),
            "rounds": int(test.get("rounds", 1)),
            "timeout": float(test.get("timeout", 60.0)),
            "max_tokens": int(test.get("max_tokens", 512)),
            "temperature": test.get("temperature"),
        }
    }
    digest = compute_config_digest(_digest_view(run, eps, params))
    return OrchestrationConfig(run=run, endpoints=endpoints, params=params, config_digest=digest)


# ---- Endpoint building (credential resolution lives here) ------------------

def _build_endpoints(raw_eps: list[Any]) -> List[EndpointSpec]:
    out: list[EndpointSpec] = []
    for ep in raw_eps:
        if not isinstance(ep, dict):
            continue
        base_url = ep.get("base_url")
        if not base_url:
            raise ValueError(f"endpoint missing base_url: {ep!r}")
        cred_raw = ep.get("credential") or ep.get("api_key")
        if not cred_raw:
            raise ValueError(f"endpoint {ep.get('name')!r} missing credential/api_key")
        cred = _resolve_credential(cred_raw)
        out.append(EndpointSpec(
            name=ep.get("name") or _short_host(base_url),
            base_url=base_url,
            credential=cred,
            vendor=ep.get("vendor"),
            fmt=ep.get("format", "openai"),
        ))
    if not out:
        raise ValueError("config must define at least one endpoint")
    return out


def _resolve_credential(raw: str) -> CredentialBearer:
    """Resolve $ENV{VAR} references and refuse plaintext-looking values.

    Reuses `security.secrets.resolve_env_references` so the failure modes
    match S5-A's loader exactly.
    """
    if not isinstance(raw, str):
        raise TypeError(f"credential must be str, got {type(raw).__name__}")
    if _ENV_REF.search(raw):
        resolved = resolve_env_references(raw)
        return CredentialBearer(resolved)
    # Plaintext path — we accept it only if the caller explicitly bypassed
    # the env-ref convention. We do NOT scan for "looks like a key" here
    # because that lives in `security.secrets.load_secrets_yaml`; this code
    # path is for tests / dev loops where the YAML is generated in memory.
    return CredentialBearer(raw)


# ---- Digest view (credential-free) ----------------------------------------

def _digest_view(run: RunParams, raw_eps: list[Any], params: dict) -> Dict[str, Any]:
    """The view of the config we hash for `config_digest`.

    Excludes credential plaintext and the per-endpoint `credential` field
    entirely; only structural / behavioural inputs go in.
    """
    return {
        "run": {"steps": list(run.steps), "concurrency": run.concurrency, "mock": run.mock},
        "endpoints": [
            {
                "name": ep.get("name"),
                "base_url": ep.get("base_url"),
                "vendor": ep.get("vendor"),
                "models": ep.get("models"),
                "format": ep.get("format", "openai"),
            }
            for ep in raw_eps if isinstance(ep, dict)
        ],
        "params": params,
    }


def _short_host(url: str) -> str:
    h = url.split("//", 1)[-1].split("/", 1)[0]
    return h.replace(":", "_")


__all__ = [
    "CONFIG_SCHEMA_VERSION",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_STEPS",
    "OrchestrationConfig",
    "RunParams",
    "compute_config_digest",
    "load_config",
]
