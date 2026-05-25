"""S1 probe-core public entry point (Story-1 contract stub).

Story-1 (TES-149) only freezes the *interface* of the ``run`` entry. The
actual two-stage parallel orchestration of P1/P2/P3/P4 lands in Story-6
(TES-145..148 / TES-150 pending). Importing ``run`` here at Story-1 means
downstream slices and PRD §6.2 A1 can already validate the call shape
without waiting on Story-2..Story-6.
"""

from __future__ import annotations

from .types import (
    PROBE_STATUS,
    PROBE_VERDICT,
    AuthSniffResult,
    InfraHint,
    ModelsDiffResult,
    ProbeError,
    ProbeReport,
    RateLimitResult,
    ReachabilityResult,
)


def run(
    base_url: str,
    key: str,
    *,
    vendor_hint: str = "auto",
    proxy: str | None = None,
    rate_limit_probe_enabled: bool = True,
    timeout_per_probe_s: int = 8,
    fail_fast: bool = True,
    models_ref_url: str | None = None,
) -> ProbeReport:
    """Run the four probe-core probes against an endpoint and return a ``ProbeReport``.

    The body of this function is implemented in Story-6 (aggregator +
    audit.py Step-0 integration). Story-1 freezes the call signature so
    downstream design slices can review the shape now.

    Parameters
    ----------
    base_url
        Full URL (with scheme; with or without trailing ``/v1``). Normalised
        internally per PRD §3.4 + R4.
    key
        Real bearer / API key. Empty string triggers the "anonymous probe"
        branch where only P1 (reachability) is executed.
    vendor_hint
        ``"auto" | "openai" | "anthropic" | "gemini"`` — controls which
        ``references/official_models_<vendor>.json`` P3 diffs against.
    proxy
        Optional ``httpx`` proxy URL; reuses ``api_relay_audit.client``'s
        transport (PRD §5.2 N3).
    rate_limit_probe_enabled
        Default ``True``. CI / cautious users set ``False`` to skip P4
        token-spending burst (PRD §5.1 C4).
    timeout_per_probe_s
        Per-probe wall-clock cap. Default 8s (PRD §5.2 N1).
    fail_fast
        Default ``True``. When ``False`` (CLI ``--no-fail-fast``), P3/P4
        still execute even if P1/P2 fail; their results get
        ``status="degraded"`` (design §3.3).
    models_ref_url
        Optional override for the official-models reference clean-up data
        source (CLI ``--models-ref-url``).

    Returns
    -------
    ProbeReport
        Aggregated four-probe result. Caller uses ``report.has_fatal()`` to
        decide whether to short-circuit downstream audit steps; the JSON
        payload comes from ``report.to_dict()``.
    """
    raise NotImplementedError(
        "probe.run() body lands in Story-6 (aggregator + audit.py Step-0). "
        "Story-1 ships the ProbeReport dataclass / JSON schema contract only."
    )


__all__ = [
    "run",
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
