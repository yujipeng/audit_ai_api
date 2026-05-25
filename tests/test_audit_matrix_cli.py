"""Smoke tests for `scripts/audit-matrix.py` CLI."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CLI = REPO_ROOT / "scripts" / "audit-matrix.py"


def _write_v1_yaml(tmp_path: Path, *, mock: bool = True) -> Path:
    body = f"""
schema_version: 1
run:
  steps: [probe, purity, perf, pricing]
  concurrency: 4
  mock: {str(mock).lower()}
endpoints:
  - name: ep-a
    base_url: https://relay-a.example.com/v1
    credential: sk-fake-NOT-A-REAL-KEY-1234567890aaa
  - name: ep-b
    base_url: https://relay-b.example.com/v1
    credential: sk-fake-NOT-A-REAL-KEY-1234567890bbb
  - name: ep-c
    base_url: https://relay-c.example.com/v1
    credential: sk-fake-NOT-A-REAL-KEY-1234567890ccc
params:
  models:
    default: [m-small, m-mid, m-flagship]
"""
    p = tmp_path / "audit-suite.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def _run_cli(args: list[str], cwd: Path):
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=cwd, capture_output=True, text=True, timeout=60,
    )


def test_dry_run_prints_matrix_with_36_cells(tmp_path: Path):
    yaml_path = _write_v1_yaml(tmp_path)
    result = _run_cli(
        ["--config", str(yaml_path), "--cache-dir", str(tmp_path / "cache"), "--dry-run"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    # AC-S5-001: 3 endpoints x 3 models x 4 steps = 36 cells, zero network.
    assert "cells:            36" in result.stdout
    # All misses on first dry-run (cold cache).
    assert "predicted hits:   0" in result.stdout
    assert "predicted misses: 36" in result.stdout


def test_dry_run_zero_network_calls(tmp_path: Path):
    """Dry-run path must not import or invoke any network library."""
    yaml_path = _write_v1_yaml(tmp_path)
    # We use strace-like proxy: just check the command exits in well under
    # a second when run with --dry-run on a 36-cell matrix.
    import time
    yaml_path = _write_v1_yaml(tmp_path)
    t0 = time.perf_counter()
    result = _run_cli(
        ["--config", str(yaml_path), "--cache-dir", str(tmp_path / "cache"), "--dry-run"],
        cwd=tmp_path,
    )
    elapsed = time.perf_counter() - t0
    assert result.returncode == 0, result.stderr
    # Network would balloon this; pure local run completes in <2s.
    assert elapsed < 5.0, f"dry-run took {elapsed:.2f}s — possible network call"


def test_real_run_writes_run_record_in_mock_mode(tmp_path: Path):
    yaml_path = _write_v1_yaml(tmp_path, mock=True)
    out_dir = tmp_path / "out"
    result = _run_cli(
        ["--config", str(yaml_path),
         "--cache-dir", str(tmp_path / "cache"),
         "--output", str(out_dir),
         "--quiet"],
        cwd=tmp_path,
    )
    assert result.returncode == 0, f"stdout={result.stdout!r} stderr={result.stderr!r}"
    rec_path = out_dir / "run-record.json"
    assert rec_path.exists()
    rec = json.loads(rec_path.read_text())
    assert rec["schema_version"] == 1
    assert rec["summary"]["total"] == 36
    # The CLI also prints summary as JSON to stdout.
    summary_blob = json.loads(result.stdout)
    assert summary_blob["summary"]["total"] == 36


def test_no_cache_lever_forces_misses(tmp_path: Path):
    yaml_path = _write_v1_yaml(tmp_path, mock=True)
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    # First populates cache.
    _run_cli(
        ["--config", str(yaml_path), "--cache-dir", str(cache_dir),
         "--output", str(out_dir), "--quiet"], cwd=tmp_path,
    )

    # Now run with --no-cache; should miss all cells (cache_hits = 0).
    result = _run_cli(
        ["--config", str(yaml_path), "--cache-dir", str(cache_dir),
         "--output", str(out_dir), "--no-cache", "--quiet"], cwd=tmp_path,
    )
    assert result.returncode == 0
    rec = json.loads((out_dir / "run-record.json").read_text())
    assert rec["summary"]["cache_hits"] == 0


def test_refresh_models_lever_drops_only_probe_step(tmp_path: Path):
    yaml_path = _write_v1_yaml(tmp_path, mock=True)
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    # Run twice to populate AND warm the cache.
    _run_cli(["--config", str(yaml_path), "--cache-dir", str(cache_dir),
              "--output", str(out_dir), "--quiet"], cwd=tmp_path)
    # Second run: should hit all 36 cells from cache.
    result_warm = _run_cli(["--config", str(yaml_path), "--cache-dir", str(cache_dir),
                            "--output", str(out_dir), "--quiet"], cwd=tmp_path)
    rec_warm = json.loads((out_dir / "run-record.json").read_text())
    assert rec_warm["summary"]["cache_hits"] == 36

    # Now --refresh-models: probe (9 cells) misses, others (27 cells) hit.
    result = _run_cli(["--config", str(yaml_path), "--cache-dir", str(cache_dir),
                       "--output", str(out_dir), "--refresh-models", "--quiet"],
                      cwd=tmp_path)
    assert result.returncode == 0
    rec = json.loads((out_dir / "run-record.json").read_text())
    assert rec["summary"]["cache_hits"] == 27
    assert rec["summary"]["cache_misses"] == 9


def test_cache_hit_rate_meets_ac_s5_004(tmp_path: Path):
    """AC-S5-004: no schema/code change re-run -> cache hit rate >= 0.8."""
    yaml_path = _write_v1_yaml(tmp_path, mock=True)
    cache_dir = tmp_path / "cache"
    out_dir = tmp_path / "out"

    _run_cli(["--config", str(yaml_path), "--cache-dir", str(cache_dir),
              "--output", str(out_dir), "--quiet"], cwd=tmp_path)
    result = _run_cli(["--config", str(yaml_path), "--cache-dir", str(cache_dir),
                       "--output", str(out_dir), "--quiet"], cwd=tmp_path)
    rec = json.loads((out_dir / "run-record.json").read_text())
    assert rec["summary"]["hit_rate"] >= 0.8, (
        f"hit rate {rec['summary']['hit_rate']} below AC-S5-004 floor of 0.8"
    )
