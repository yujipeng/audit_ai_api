"""Tests for orchestration.cache — key formula + TTL/LRU + four invalidation levers."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from orchestration.cache import (
    CACHE_KEY_FORMULA_VERSION,
    Cache,
    compute_cache_key,
    endpoint_id,
    predict_cache,
)


# ---- Key formula ----------------------------------------------------------

def test_cache_key_formula_version_constant():
    assert CACHE_KEY_FORMULA_VERSION == 1


def test_endpoint_id_is_deterministic_and_credential_free():
    a = endpoint_id("https://relay.example.com/v1")
    b = endpoint_id("https://relay.example.com/v1")
    assert a == b
    # Different base_url -> different id.
    c = endpoint_id("https://other.example.com/v1")
    assert a != c
    # The credential never enters the id.
    assert "sk-" not in a
    assert len(a) == 16


def test_cache_key_changes_when_schema_version_bumps():
    common = dict(
        step="probe", base_url="https://x.test", model="m",
        code_version="v1", params={"a": 1},
    )
    k1 = compute_cache_key(schema_version=1, **common)
    k2 = compute_cache_key(schema_version=2, **common)
    assert k1 != k2  # AC-S5-005 lever (3)


def test_cache_key_changes_when_code_version_bumps():
    common = dict(
        step="probe", base_url="https://x.test", model="m",
        schema_version=1, params={"a": 1},
    )
    k1 = compute_cache_key(code_version="v1", **common)
    k2 = compute_cache_key(code_version="v2", **common)
    assert k1 != k2  # AC-S5-005 lever (4)


def test_cache_key_stable_across_param_dict_order():
    base = dict(step="probe", base_url="https://x.test", model="m",
                schema_version=1, code_version="v1")
    k1 = compute_cache_key(**base, params={"a": 1, "b": 2})
    k2 = compute_cache_key(**base, params={"b": 2, "a": 1})
    assert k1 == k2  # canonical JSON sorts keys


def test_cache_key_step_separates_namespaces():
    base = dict(base_url="https://x.test", model="m",
                schema_version=1, code_version="v1", params={})
    k_probe = compute_cache_key(step="probe", **base)
    k_purity = compute_cache_key(step="purity", **base)
    assert k_probe != k_purity


# ---- TTL semantics --------------------------------------------------------

def test_cache_set_and_get_roundtrip(tmp_path: Path):
    c = Cache(tmp_path)
    c.set("k1", {"x": 1, "step": "probe"})
    got = c.get("k1")
    assert got == {"x": 1, "step": "probe"}
    assert c.hits == 1 and c.misses == 0


def test_cache_get_miss_increments_miss_counter(tmp_path: Path):
    c = Cache(tmp_path)
    assert c.get("does-not-exist") is None
    assert c.misses == 1 and c.hits == 0


def test_cache_ttl_expires_entries(tmp_path: Path):
    c = Cache(tmp_path, ttl_seconds=0)  # immediate expiry
    c.set("k", {"x": 1, "step": "probe"})
    time.sleep(0.01)  # nudge time forward past 0
    assert c.get("k") is None


# ---- Invalidation lever 1: --no-cache (disabled=True) ----------------------

def test_no_cache_lever_misses_every_lookup(tmp_path: Path):
    c = Cache(tmp_path, disabled=True)
    c.set("k", {"x": 1, "step": "probe"})  # silently dropped
    assert c.get("k") is None
    assert c.misses == 1


# ---- Invalidation lever 2: --refresh-models (drop step namespace) ---------

def test_refresh_models_lever_drops_only_probe_step(tmp_path: Path):
    c = Cache(tmp_path)
    c.set("k_probe", {"step": "probe", "models": ["x"]})
    c.set("k_purity", {"step": "purity", "score": 1.0})
    c.set("k_perf", {"step": "perf", "ttft": 0.5})
    n = c.invalidate_step("probe")
    assert n == 1
    assert c.get("k_probe") is None
    # Counter check: purity/perf survive.
    fresh = Cache(tmp_path)  # reset counters
    assert fresh.get("k_purity") == {"step": "purity", "score": 1.0}
    assert fresh.get("k_perf") == {"step": "perf", "ttft": 0.5}


# ---- LRU pruning ----------------------------------------------------------

def test_lru_prunes_oldest_entries_when_over_capacity(tmp_path: Path):
    c = Cache(tmp_path, max_entries=3)
    # Write 5 entries, sleeping briefly between each so mtimes differ.
    for i in range(5):
        c.set(f"k{i}", {"i": i, "step": "probe"})
        time.sleep(0.01)
    files = sorted(tmp_path.glob("*.json"))
    assert len(files) <= 3


# ---- Hit rate -------------------------------------------------------------

def test_hit_rate_computed_correctly(tmp_path: Path):
    c = Cache(tmp_path)
    c.set("a", {"step": "probe"})
    c.get("a")           # hit
    c.get("missing")     # miss
    c.get("a")           # hit
    assert c.hit_rate() == pytest.approx(2 / 3)


def test_hit_rate_zero_when_no_lookups(tmp_path: Path):
    c = Cache(tmp_path)
    assert c.hit_rate() == 0.0


# ---- predict_cache (used by --dry-run) ------------------------------------

def test_predict_cache_reports_all_miss_for_empty_cache(tmp_path: Path):
    c = Cache(tmp_path)
    cells = [
        ("probe", "https://x.test", "m1", 1, "v1", {"r": 1}),
        ("probe", "https://x.test", "m2", 1, "v1", {"r": 1}),
    ]
    pred = predict_cache(c, cells)
    assert pred == {"hits": 0, "misses": 2, "total": 2}


def test_predict_cache_reports_hit_after_set(tmp_path: Path):
    c = Cache(tmp_path)
    cells = [("probe", "https://x.test", "m1", 1, "v1", {"r": 1})]
    key = compute_cache_key(
        step="probe", base_url="https://x.test", model="m1",
        schema_version=1, code_version="v1", params={"r": 1},
    )
    c.set(key, {"step": "probe", "payload": "x"})
    pred = predict_cache(c, cells)
    assert pred == {"hits": 1, "misses": 0, "total": 1}


def test_predict_cache_does_not_mutate_counters(tmp_path: Path):
    c = Cache(tmp_path)
    c.set("foo", {"step": "probe"})
    cells = [("probe", "https://x.test", "m1", 1, "v1", {})]
    predict_cache(c, cells)
    # The act of predicting shouldn't count as a real lookup.
    assert c.hits == 0 and c.misses == 0


def test_predict_cache_disabled_means_all_miss(tmp_path: Path):
    c = Cache(tmp_path, disabled=True)
    key = compute_cache_key(
        step="probe", base_url="https://x.test", model="m1",
        schema_version=1, code_version="v1", params={},
    )
    c.set(key, {"step": "probe"})  # silently dropped
    cells = [("probe", "https://x.test", "m1", 1, "v1", {})]
    pred = predict_cache(c, cells)
    assert pred == {"hits": 0, "misses": 1, "total": 1}
