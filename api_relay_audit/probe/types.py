"""S1 probe-core dataclass + JSON Schema freeze.

Story-1 (TES-149) ships the *contract surface only* for the probe-core
subsystem. Implementations (P1 reachability / P2 auth_sniff / P3
models_diff / P4 rate_limit_fp / aggregator / audit.py Step-0 integration)
land in Story-2..Story-6.

The dataclasses below freeze the JSON shape declared in
``docs/prd/2026-05-24-audit-ai-relay-prd-probe-core.md`` §3.5 and
``docs/design/2026-05-24-audit-ai-relay-design-probe-core.md`` §4.1.
Downstream slices (S2 purity-eval / S3 perf-bench-v2 / S4
pricing-compliance / S5 orchestration-report) consume ONLY this module's
exports; they MUST NOT import from sibling probe submodules.

Freeze rules (design §4.5):

- adding a field with a default is allowed (forward-compatible);
- removing or renaming a field requires a v5 design cycle;
- ``schema_version`` is character-pinned to ``"1.0"``;
- raw key material MUST NOT appear in any field — only sha256 prefix
  via ``input_key_fingerprint`` (PRD §4.2 / §5.1 C2 / §6.2 A8).
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Optional


# Per-probe status, independent of the aggregated verdict.
# PRD §4.2 mandates tri-state and forbids swallowing errors via raise.
PROBE_STATUS = ("ok", "degraded", "error")

# Aggregated verdict, computed by the Story-6 aggregator.
# PRD §3.3 / §6.2: "fail" makes audit.py exit code 2 and short-circuits.
PROBE_VERDICT = ("pass", "warn", "fail")


@dataclass
class ProbeError:
    """PRD §4.2: every ProbeResult error MUST carry a code and a message.

    ``code`` is a short machine-readable identifier (e.g. ``tcp_refused``,
    ``tls_handshake_failed``, ``401_returned``). ``message`` is a
    human-readable string already passed through transparent_log redaction.
    """

    code: str
    message: str


@dataclass
class ReachabilityResult:
    """P1 reachability probe output (Story-2 implementation)."""

    status: str
    tcp_ok: bool = False
    tls_ok: bool = False
    tls_chain_summary: Optional[str] = None
    dns_resolves: bool = True
    http_status_root: Optional[int] = None
    latency_ms: Optional[int] = None
    fallback_to_curl: bool = False
    signals: list = field(default_factory=list)
    error: Optional[ProbeError] = None


@dataclass
class AuthSniffResult:
    """P2 auth-sniff probe output (Story-3 implementation)."""

    status: str
    accepted_schemes: list = field(default_factory=list)
    envelope_401: str = "unknown"
    envelope_403: str = "unknown"
    key_position: str = "unknown"
    classification: str = "unknown"
    signals: list = field(default_factory=list)
    error: Optional[ProbeError] = None


@dataclass
class ModelsDiffResult:
    """P3 models-list-diff probe output (Story-4 implementation)."""

    status: str
    declared: list = field(default_factory=list)
    declared_count: int = 0
    official_reference: list = field(default_factory=list)
    catalog_version: Optional[str] = None
    extra_in_relay: list = field(default_factory=list)
    missing_in_relay: list = field(default_factory=list)
    suspicious_aliases: list = field(default_factory=list)
    claimed_model_match: str = "unknown"
    vendor_breakdown: dict = field(default_factory=dict)
    signals: list = field(default_factory=list)
    error: Optional[ProbeError] = None


@dataclass
class RateLimitResult:
    """P4 rate-limit-fingerprint probe output (Story-5 implementation)."""

    status: str
    rpm_observed: Optional[int] = None
    headers_seen: list = field(default_factory=list)
    envelope_429: str = "absent"
    retry_after_pattern: str = "unknown"
    burst_window_s: Optional[int] = None
    triggered_429: bool = False
    samples_to_429: Optional[int] = None
    compliance: str = "unknown"
    probe_disabled: bool = False
    signals: list = field(default_factory=list)
    error: Optional[ProbeError] = None


@dataclass
class InfraHint:
    """Derived ``infra_fingerprint.classify_framework()`` snapshot.

    PRD §3.5 marks this as an optional field on ProbeReport; the
    aggregator (Story-6) populates it after running Step 12 to avoid a
    second pass at report time.
    """

    framework: str
    confidence: str


def _serialize(value):
    """Recursive serializer for dataclasses → JSON-friendly primitives.

    Drops ``None`` for optional fields whose absence is meaningful
    (Story-1: ``infra_hint``). Sub-dataclasses recurse; lists/dicts are
    walked element-wise.
    """
    if is_dataclass(value):
        out = {}
        for f in fields(value):
            v = getattr(value, f.name)
            out[f.name] = _serialize(v)
        return out
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


@dataclass
class ProbeReport:
    """Aggregated probe-core output (PRD §3.5 schema).

    ``schema_version`` is character-pinned to :pyattr:`SCHEMA_VERSION`.
    Construction never accepts a raw key; ``input_key_fingerprint`` is
    the only key-derived field (PRD §4.2 / §5.1 C2).
    """

    SCHEMA_VERSION = "1.0"

    schema_version: str
    generated_at: str
    input_base_url: str
    input_key_fingerprint: str
    input_vendor_hint: str
    verdict: str
    reachability: ReachabilityResult
    auth_sniff: AuthSniffResult
    models_diff: ModelsDiffResult
    rate_limit: RateLimitResult
    infra_hint: Optional[InfraHint] = None
    total_http_calls: int = 0

    def has_fatal(self) -> bool:
        """True iff aggregated verdict is ``"fail"`` (PRD §3.3 short-circuit)."""
        return self.verdict == "fail"

    def to_dict(self) -> dict:
        """Serialize to a PRD §3.5-shaped dict.

        Absent optional fields (currently only ``infra_hint``) are omitted
        rather than emitted as ``null`` so downstream JSON consumers don't
        need to distinguish "missing" from "explicit null".
        """
        payload = {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "input_base_url": self.input_base_url,
            "input_key_fingerprint": self.input_key_fingerprint,
            "input_vendor_hint": self.input_vendor_hint,
            "verdict": self.verdict,
            "reachability": _serialize(self.reachability),
            "auth_sniff": _serialize(self.auth_sniff),
            "models_diff": _serialize(self.models_diff),
            "rate_limit": _serialize(self.rate_limit),
            "total_http_calls": self.total_http_calls,
        }
        if self.infra_hint is not None:
            payload["infra_hint"] = _serialize(self.infra_hint)
        return payload


__all__ = [
    "PROBE_STATUS",
    "PROBE_VERDICT",
    "ProbeError",
    "ReachabilityResult",
    "AuthSniffResult",
    "ModelsDiffResult",
    "RateLimitResult",
    "InfraHint",
    "ProbeReport",
]
