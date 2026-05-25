"""S1 probe-core / P3 models-list-diff (Story-4, TES-152, TES-97).

PRD §3.1 P3: GET ``/v1/models`` and diff the relay's declared catalog
against a human-curated official-vendor reference. Emits three diff
classes per PRD §6.2 A6:

- **extra_in_relay**     — IDs the relay advertises that no vendor ships.
- **missing_in_relay**   — flagship IDs the user expects from the
  vendor(s) implied by ``vendor_hint`` but the relay omits.
- **suspicious_aliases** — heuristic-derived subset of extras that match
  known model-name laundering patterns (CISPA arXiv:2603.01919 §3.2).

Token-cost: zero. Only one ``GET /v1/models`` call goes out, dispatched
by ``client.get_models()`` (which already exists in
``api_relay_audit/client.py``). This module's only side effect is HTTP.

Reference catalogs ship as static JSON under
``api_relay_audit/probe/references/official_models_<vendor>.json`` and
can be overridden per-call via ``models_ref_url`` (CLI
``--models-ref-url``, PRD §10).

This is the modular distribution. The standalone ``audit.py`` mirrors
the diff helpers + the suspicious-alias regex inside its
``# === models_diff helpers ===`` Section block; the parity check lives
in ``tests/test_dual_distribution_parity.py``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse
from urllib.request import urlopen

from .types import ModelsDiffResult, ProbeError


_REFERENCES_DIR = Path(__file__).resolve().parent / "references"

# vendor_hint values that map 1:1 to a reference catalog file.
_PER_VENDOR_HINTS = ("openai", "anthropic", "gemini")


# ---------------------------------------------------------------------------
# suspicious_alias heuristic (PRD §3.1 P3 + fixture F3 seeds)
# ---------------------------------------------------------------------------
#
# Each pattern is documented with a ``diff_class`` from
# ``tests/fixtures/models_diff/suspicious_alias_counterexamples.json`` so
# reviewers can map a flagged ID back to the F3 taxonomy without
# guessing. PM curates the seeds; dev MAY extend here and MUST add new
# positives back to the fixture so coverage stays in sync (see fixture
# README ``review_boundary``).

_SUSPICIOUS_ALIAS_PATTERNS: tuple[tuple[str, str], ...] = (
    # tier_confusion_cross_vendor — GPT borrowing Gemini/Claude tiers
    (r"^gpt-\d+(\.\d+)?-(pro|ultra|plus)$", "tier_confusion_cross_vendor"),
    (r"^gpt-\d+(\.\d+)?-(ultra|max)$", "tier_confusion_cross_vendor"),
    # stacked_tier_suffix — two tier modifiers stacked
    (
        r"-(turbo-max|max-turbo|turbo-plus|plus-turbo|pro-max|max-pro)$",
        "stacked_tier_suffix",
    ),
    # tier_confusion_cross_vendor — Claude borrowing OpenAI/Gemini tiers
    (
        r"^claude-\d+(-\d+)?-(mini|nano|micro|flash|turbo)$",
        "tier_confusion_cross_vendor",
    ),
    # version_forward_fake — Claude major version > 4
    (r"^claude-(opus|sonnet|haiku)-([5-9]|\d{2,})$", "version_forward_fake"),
    # version_forward_fake — Gemini major version > 2
    (r"^gemini-([3-9]|\d{2,})\.\d+", "version_forward_fake"),
    # stacked_tier_suffix — Gemini stacked tier
    (
        r"^gemini-(pro-max|pro-plus|flash-max|flash-pro)$",
        "stacked_tier_suffix",
    ),
    # gpt-4-turbo-YYYY without a real release suffix (gpt-4-turbo-2024
    # vs the legit gpt-4-turbo-2024-04-09). The legit form has a full
    # YYYY-MM-DD; the laundering form stops at YYYY.
    (r"^gpt-4-turbo-\d{4}$", "tier_confusion_cross_vendor"),
)

_SUSPICIOUS_ALIAS_REGEXES = tuple(
    (re.compile(pat), label) for pat, label in _SUSPICIOUS_ALIAS_PATTERNS
)


def is_suspicious_alias(model_id: str) -> bool:
    """Return True iff ``model_id`` matches any suspicious-alias pattern.

    Patterns are seeded from
    ``tests/fixtures/models_diff/suspicious_alias_counterexamples.json``.
    """
    if not isinstance(model_id, str) or not model_id:
        return False
    for regex, _label in _SUSPICIOUS_ALIAS_REGEXES:
        if regex.search(model_id):
            return True
    return False


def _classify_vendor(model_id: str) -> Optional[str]:
    """Best-effort vendor bucket by ID prefix; ``None`` for unknowns."""
    if not isinstance(model_id, str):
        return None
    if model_id.startswith(("gpt-", "o1", "o3", "o4", "text-embedding-", "whisper-")):
        return "openai"
    if model_id.startswith("claude-"):
        return "anthropic"
    if model_id.startswith(("gemini-", "embedding-", "aqa", "text-embedding-004")):
        return "gemini"
    return None


# ---------------------------------------------------------------------------
# catalog loading
# ---------------------------------------------------------------------------


def _load_catalog_file(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _load_catalog_url(ref_url: str) -> dict:
    """Load a catalog from either a local file path or an http(s) URL.

    Local paths are accepted so tests / users without network can point
    ``--models-ref-url`` at a file. URL fetches go through stdlib
    ``urlopen`` (no extra dependency) with a 5 s timeout.
    """
    parsed = urlparse(ref_url)
    if parsed.scheme in ("http", "https"):
        with urlopen(ref_url, timeout=5) as resp:  # noqa: S310 — user opt-in
            payload = resp.read().decode("utf-8")
        return json.loads(payload)
    return _load_catalog_file(Path(ref_url))


def _resolve_reference_catalog(
    vendor_hint: str,
    models_ref_url: Optional[str],
) -> tuple[dict, str]:
    """Return ``(catalog_dict, primary_vendor)`` for the requested hint.

    For ``vendor_hint='auto'`` the three per-vendor catalogs are merged
    into a virtual catalog whose ``models`` and ``top_priority`` are
    unions; ``catalog_version`` is taken from the openai catalog (all
    three currently share the same version, but if they diverge we want
    a deterministic primary).
    """
    if models_ref_url:
        catalog = _load_catalog_url(models_ref_url)
        return catalog, catalog.get("vendor", vendor_hint)

    if vendor_hint in _PER_VENDOR_HINTS:
        path = _REFERENCES_DIR / f"official_models_{vendor_hint}.json"
        return _load_catalog_file(path), vendor_hint

    # auto: union all three
    merged_models: list[str] = []
    merged_top: list[str] = []
    primary_version: Optional[str] = None
    for v in _PER_VENDOR_HINTS:
        sub = _load_catalog_file(_REFERENCES_DIR / f"official_models_{v}.json")
        merged_models.extend(sub.get("models", []))
        merged_top.extend(sub.get("top_priority", []))
        if v == "openai":
            primary_version = sub.get("catalog_version")
    return (
        {
            "catalog_version": primary_version,
            "vendor": "auto",
            "models": merged_models,
            "top_priority": merged_top,
        },
        "auto",
    )


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------


def _extract_declared_ids(payload) -> list[str]:
    """Pull the ``id`` field out of a ``client.get_models()`` payload.

    The real ``client.get_models`` returns a list of dicts shaped like
    ``{"id": "gpt-4o", "object": "model", ...}``. This helper trusts
    that shape and skips entries missing ``id`` (rather than raising)
    so a partially-malformed relay listing still yields a usable diff.
    """
    if not isinstance(payload, list):
        return []
    declared: list[str] = []
    for entry in payload:
        if isinstance(entry, dict) and isinstance(entry.get("id"), str):
            declared.append(entry["id"])
    return declared


def _diff_extras(declared: Iterable[str], official: Iterable[str]) -> list[str]:
    official_set = set(official)
    return [m for m in declared if m not in official_set]


def _diff_missing_top_priority(
    declared: Iterable[str], top_priority: Iterable[str]
) -> list[str]:
    declared_set = set(declared)
    return [m for m in top_priority if m not in declared_set]


def _summarise_signals(
    declared: list[str],
    extras: list[str],
    missing: list[str],
    suspicious: list[str],
) -> list[str]:
    """Build short human-readable flag strings for the report. Kept tiny
    so the JSON payload stays compact (PRD §5.2 N2)."""
    signals: list[str] = []
    if extras:
        signals.append(f"extra_in_relay={len(extras)}")
    if missing:
        signals.append(f"missing_in_relay={len(missing)}")
    if suspicious:
        signals.append(
            f"suspicious_aliases={len(suspicious)} ("
            + ",".join(suspicious[:3])
            + ("..." if len(suspicious) > 3 else "")
            + ")"
        )
    if not signals:
        signals.append(f"declared={len(declared)} clean")
    return signals


def _claimed_match(extras: list[str], missing: list[str], suspicious: list[str]) -> str:
    """Roll the three diff classes into a P3-local verdict.

    The aggregator (Story-6) maps this onto the global ``ProbeReport``
    verdict; here we only need a stable ternary so reviewers can read a
    single field instead of recomputing.
    """
    if suspicious:
        return "fail"
    if missing or extras:
        return "warn"
    return "pass"


def fetch_models_diff(
    client,
    vendor_hint: str = "auto",
    *,
    models_ref_url: Optional[str] = None,
) -> ModelsDiffResult:
    """P3 entry point — diff a relay's ``/v1/models`` listing against
    the curated official reference for ``vendor_hint``.

    Parameters
    ----------
    client
        ``api_relay_audit.client.APIClient`` instance (or any object
        exposing ``.get_models() -> list[dict]``). Tests inject a
        ``MagicMock``.
    vendor_hint
        ``"auto" | "openai" | "anthropic" | "gemini"``. ``auto`` unions
        all three reference catalogs, scoping ``missing_in_relay`` to
        flagships from any vendor. Per-vendor hints scope the diff so a
        relay that only advertises openai models doesn't get faulted
        for missing claude flagships.
    models_ref_url
        Optional override (PRD §10 ``--models-ref-url``). May point at
        an http(s) URL or a local file path.

    Returns
    -------
    ModelsDiffResult
        Per PRD §3.5 / §4.1. ``status='ok'`` on transport success even
        when the diff fires; ``claimed_model_match`` is the diff-local
        verdict.
    """
    try:
        catalog, primary_vendor = _resolve_reference_catalog(
            vendor_hint, models_ref_url
        )
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        return ModelsDiffResult(
            status="error",
            error=ProbeError(
                code="catalog_load_failed",
                message=f"reference catalog unavailable: {exc.__class__.__name__}",
            ),
        )

    raw_payload = client.get_models()
    declared = _extract_declared_ids(raw_payload)

    if not declared:
        return ModelsDiffResult(
            status="error",
            declared=[],
            declared_count=0,
            official_reference=list(catalog.get("models", [])),
            catalog_version=catalog.get("catalog_version"),
            error=ProbeError(
                code="models_list_unavailable",
                message=(
                    "client.get_models() returned no usable entries"
                    if isinstance(raw_payload, list)
                    else "client.get_models() returned non-list payload"
                ),
            ),
        )

    official = list(catalog.get("models", []))
    top_priority = list(catalog.get("top_priority", []))

    extras = _diff_extras(declared, official)
    missing = _diff_missing_top_priority(declared, top_priority)
    suspicious = [m for m in extras if is_suspicious_alias(m)]

    breakdown: dict[str, int] = {}
    for m in declared:
        v = _classify_vendor(m)
        if v is not None:
            breakdown[v] = breakdown.get(v, 0) + 1

    return ModelsDiffResult(
        status="ok",
        declared=declared,
        declared_count=len(declared),
        official_reference=official,
        catalog_version=catalog.get("catalog_version"),
        extra_in_relay=extras,
        missing_in_relay=missing,
        suspicious_aliases=suspicious,
        claimed_model_match=_claimed_match(extras, missing, suspicious),
        vendor_breakdown=breakdown,
        signals=_summarise_signals(declared, extras, missing, suspicious),
    )


__all__ = [
    "fetch_models_diff",
    "is_suspicious_alias",
]
