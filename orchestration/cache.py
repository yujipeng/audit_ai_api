"""Step-level cache — key formula, TTL/LRU store, four invalidation levers.

The cache exists to make repeated audit runs cheap when nothing meaningful has
changed. "Nothing meaningful" is the whole point: the key formula must
*include* everything that could legitimately change the answer and *exclude*
everything that couldn't (e.g. wall-clock time, the credential value itself).

Key formula (frozen — bumping this is a schema_version bump):

    cache_key = sha256(
        step_name + "\x00" +
        endpoint_id + "\x00" +
        model + "\x00" +
        schema_version + "\x00" +
        code_version + "\x00" +
        params_digest
    )

Where:
  * `endpoint_id` is `sha256(base_url)[:16]` — derived from the URL, never the
    credential. Two endpoints with the same base_url collide intentionally:
    they would return the same answer.
  * `params_digest` is `sha256(json_canonical(params))[:16]`.
  * `schema_version` / `code_version` come from the step adapter module
    constants in `orchestration.steps` (PROBE_SCHEMA_VERSION, ...).

Four invalidation levers (AC-S5-005):

  1. `--no-cache` CLI flag         → `Cache.disabled=True`, every lookup miss.
  2. `--refresh-models` CLI flag   → drops the `probe` step's namespace only.
  3. step adapter `schema_version` → changing the constant invalidates that
                                     step's cached values automatically.
  4. step adapter `code_version`   → ditto, for non-schema code changes.

TTL/LRU semantics:
  * TTL is a per-entry timestamp; expired entries are missed and removed on
    access.
  * LRU is approximate: when the on-disk store exceeds `max_entries`, the
    oldest-by-mtime entries are pruned. Approximate is fine — the cache is a
    perf optimisation, not a correctness boundary.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

CACHE_KEY_FORMULA_VERSION = 1
DEFAULT_TTL_SECONDS = 7 * 24 * 3600  # one week
DEFAULT_MAX_ENTRIES = 4096


def _sha256_short(data: str, *, n: int = 16) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()[:n]


def _canonical_json(obj: Any) -> str:
    """Stable serialisation for hashing. sort_keys + no whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def endpoint_id(base_url: str) -> str:
    """Derive a deterministic id from the endpoint URL.

    Critical property: the credential is NEVER an input. Two endpoints with
    the same base_url but different keys legitimately share the same cached
    answer.
    """
    return _sha256_short(base_url, n=16)


def compute_cache_key(
    *,
    step: str,
    base_url: str,
    model: str,
    schema_version: int,
    code_version: str,
    params: Dict[str, Any],
) -> str:
    """Public key formula. See module docstring for the contract."""
    params_digest = _sha256_short(_canonical_json(params))
    parts = [step, endpoint_id(base_url), model, str(schema_version), code_version, params_digest]
    raw = "\x00".join(parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class _Entry:
    """An on-disk cache entry. Key is in the filename, payload is the JSON file."""

    key: str
    written_at: float
    payload: Dict[str, Any]


class Cache:
    """A simple filesystem cache; one JSON file per key, atomic write.

    The on-disk format is deliberately boring so the credential-scan CI gate
    can rip through it without false positives.
    """

    def __init__(
        self,
        cache_dir: Path | str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        disabled: bool = False,
    ) -> None:
        self._dir = Path(cache_dir)
        self._ttl = int(ttl_seconds)
        self._max = int(max_entries)
        self.disabled = bool(disabled)
        self.hits = 0
        self.misses = 0

    # ---- Public API -------------------------------------------------------

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        if self.disabled:
            self.misses += 1
            return None
        path = self._path_for(key)
        if not path.exists():
            self.misses += 1
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.misses += 1
            return None
        if not isinstance(data, dict):
            self.misses += 1
            return None
        written_at = float(data.get("__written_at", 0))
        if (time.time() - written_at) > self._ttl:
            # Expired — pretend it isn't there and clean up lazily.
            try:
                path.unlink()
            except OSError:
                pass
            self.misses += 1
            return None
        self.hits += 1
        return data.get("payload") if isinstance(data.get("payload"), dict) else None

    def set(self, key: str, payload: Dict[str, Any], *, step: Optional[str] = None) -> None:
        if self.disabled:
            return
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._path_for(key)
        record = {
            "__formula_version": CACHE_KEY_FORMULA_VERSION,
            "__written_at": time.time(),
            "__step": step,
            "payload": payload,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(_canonical_json(record), encoding="utf-8")
        os.replace(tmp, path)
        self._prune_if_over_capacity()

    def invalidate_step(self, step: str) -> int:
        """Drop every entry whose stored step metadata matches.

        Used by `--refresh-models`, which clears just the `probe` namespace.
        We record the step as a top-level metadata field at `set` time so the
        invalidator never has to grovel into the opaque adapter payload.
        """
        if not self._dir.exists():
            return 0
        n_removed = 0
        for path in self._dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                stored_step = data.get("__step")
                # Backwards compat: older entries may have step nested in payload.
                if stored_step is None:
                    payload = data.get("payload") or {}
                    if isinstance(payload, dict):
                        stored_step = payload.get("step")
                if stored_step == step:
                    path.unlink()
                    n_removed += 1
            except (OSError, json.JSONDecodeError):
                continue
        return n_removed

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return (self.hits / total) if total else 0.0

    # ---- Internals --------------------------------------------------------

    def _path_for(self, key: str) -> Path:
        # Two-char shard to keep directory sizes bounded.
        return self._dir / f"{key[:64]}.json"

    def _prune_if_over_capacity(self) -> None:
        try:
            files = list(self._dir.glob("*.json"))
        except OSError:
            return
        if len(files) <= self._max:
            return
        # Sort by mtime ascending — oldest first.
        files.sort(key=lambda p: p.stat().st_mtime)
        excess = len(files) - self._max
        for path in files[:excess]:
            try:
                path.unlink()
            except OSError:
                pass


def predict_cache(
    cache: Cache,
    cells: list[Tuple[str, str, str, int, str, Dict[str, Any]]],
) -> Dict[str, int]:
    """For `--dry-run`: walk a list of (step, base_url, model, schema_v, code_v, params)
    tuples and report predicted hit / miss counts without changing cache state.
    """
    hits = 0
    misses = 0
    for step, base_url, model, schema_version, code_version, params in cells:
        key = compute_cache_key(
            step=step, base_url=base_url, model=model,
            schema_version=schema_version, code_version=code_version, params=params,
        )
        # Don't use cache.get — that mutates hit/miss counters and may delete
        # expired entries. Probe the file directly.
        path = cache._path_for(key)
        if cache.disabled or not path.exists():
            misses += 1
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (time.time() - float(data.get("__written_at", 0))) > cache._ttl:
                misses += 1
            else:
                hits += 1
        except (OSError, json.JSONDecodeError):
            misses += 1
    return {"hits": hits, "misses": misses, "total": len(cells)}


__all__ = [
    "CACHE_KEY_FORMULA_VERSION",
    "Cache",
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "compute_cache_key",
    "endpoint_id",
    "predict_cache",
]
