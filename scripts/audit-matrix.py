#!/usr/bin/env python3
"""audit-matrix — entry point for the audit-suite orchestrator (TES-128 S5-B).

Three usage modes:

  1. Dry run (AC-S5-001) — print the matrix shape + predicted cache hits,
     zero network I/O::

         audit-matrix.py --config audit-suite.yaml --dry-run

  2. Mock run (AC-S5-003) — run all step adapters in mock mode against the
     configured matrix and produce a run-record.json::

         audit-matrix.py --config audit-suite.yaml --output reports/

  3. Real run — same as mock but with `mock: false` in the YAML.

CLI cache levers (AC-S5-005):
  --no-cache         — bypass cache entirely (every cell is a miss)
  --refresh-models   — drop the `probe` step's cached entries before running
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Make sibling modules importable when run as `python scripts/audit-matrix.py`.
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from orchestration.cache import Cache, compute_cache_key, predict_cache  # noqa: E402
from orchestration.config import load_config  # noqa: E402
from orchestration.run_record import RunRecordWriter  # noqa: E402
from orchestration.runner import run_matrix  # noqa: E402
from orchestration.steps import DEFAULT_ADAPTERS  # noqa: E402


DEFAULT_CACHE_DIR = ".cache/audit-matrix"


def _resolve_code_version() -> str:
    """Best-effort commit sha for the run-record."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_ROOT, capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip() or "unknown"
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return "unknown"


def _enumerate_cells(cfg) -> list[tuple]:
    """Yield (step, base_url, model, schema_version, code_version, params) tuples."""
    import orchestration.steps as steps_mod
    out: list[tuple] = []
    for step in cfg.run.steps:
        adapter = DEFAULT_ADAPTERS.get(step)
        if adapter is None:
            continue
        name = adapter.__name__.removeprefix("step_").upper()
        sv = int(getattr(steps_mod, f"{name}_SCHEMA_VERSION", 1))
        cv = str(getattr(steps_mod, f"{name}_CODE_VERSION", "unknown"))
        step_params = dict((cfg.params or {}).get(step, {}))
        step_params["mock"] = bool(cfg.run.mock)
        for ep in cfg.endpoints:
            models = ((cfg.params or {}).get("models") or {}).get(ep.name)
            if not models:
                models = ((cfg.params or {}).get("models") or {}).get("default") or ["default"]
            for m in models:
                out.append((step, ep.base_url, m, sv, cv, step_params))
    return out


def _do_dry_run(cfg, cache: Cache) -> int:
    cells = _enumerate_cells(cfg)
    prediction = predict_cache(cache, cells)

    print(f"audit-matrix dry-run")
    print(f"  config:           {cfg.source_path}")
    print(f"  config_digest:    {cfg.config_digest}")
    print(f"  steps:            {list(cfg.run.steps)}")
    print(f"  endpoints:        {[ep.name for ep in cfg.endpoints]}")
    print(f"  concurrency:      {cfg.run.concurrency}")
    print(f"  mock:             {cfg.run.mock}")
    print(f"  cells:            {prediction['total']}")
    print(f"  predicted hits:   {prediction['hits']}")
    print(f"  predicted misses: {prediction['misses']}")
    print(f"  cache disabled:   {cache.disabled}")
    print()
    print("matrix:")
    for step, base_url, model, sv, cv, params in cells:
        key = compute_cache_key(
            step=step, base_url=base_url, model=model,
            schema_version=sv, code_version=cv, params=params,
        )
        print(f"  step={step:8s} base_url={base_url:<40s} model={model:<24s} key={key[:12]}...")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="audit-matrix orchestrator (TES-128 S5-B)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--config", required=True, help="Path to audit-suite YAML config")
    p.add_argument("--output", default="reports", help="Directory for run-record.json output")
    p.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR, help="Cache directory")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the matrix + predicted cache hits, exit 0; zero network I/O")
    p.add_argument("--no-cache", action="store_true",
                   help="Bypass cache entirely (every cell is a miss)")
    p.add_argument("--refresh-models", action="store_true",
                   help="Drop the probe-step cache before running")
    p.add_argument("--quiet", action="store_true", help="Suppress per-step progress log")
    args = p.parse_args(argv)

    cfg = load_config(args.config)
    cache = Cache(args.cache_dir, disabled=args.no_cache)

    if args.refresh_models:
        n = cache.invalidate_step("probe")
        if not args.quiet:
            print(f"--refresh-models: dropped {n} probe-step entries from cache",
                  file=sys.stderr)

    if args.dry_run:
        return _do_dry_run(cfg, cache)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    writer = RunRecordWriter(out_dir / "run-record.json")

    log_fn = (lambda _msg: None) if args.quiet else (lambda msg: print(msg, file=sys.stderr))
    record = run_matrix(
        cfg, cache=cache, writer=writer,
        code_version=_resolve_code_version(), log_fn=log_fn,
    )

    print(json.dumps({
        "summary": record["summary"],
        "hit_rate": record["summary"]["hit_rate"],
        "cache_hit_rate_in_session": cache.hit_rate(),
        "output": str(out_dir / "run-record.json"),
    }, indent=2))
    return 0 if record["summary"]["error"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
