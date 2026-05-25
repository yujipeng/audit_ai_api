"""P4 rate-limit fingerprint probe (S1 probe-core / Story-5, TES-153).

Sends a capped sequence of ``max_tokens=1`` chat completions and reads
the relay's rate-limit headers + envelope shape. Outputs a
``RateLimitResult`` per design §7 Story-5:

* ``compliance`` tri-state — ``openai`` (relay surfaces
  ``x-ratelimit-*`` headers, OpenAI's de-facto contract), ``anthropic``
  (``anthropic-ratelimit-*`` headers, Anthropic's contract), or
  ``absent`` (no known rate-limit headers — non-compliant relay);
* ``triggered_429`` — set the moment a 429 is observed; the probe
  STOPS immediately, never retries (PRD §6.2 A7);
* ``rpm_observed`` — best-effort request-per-minute extraction from
  the OpenAI or Anthropic ``-limit-requests`` header.

Hard caps (PRD §5.1 C4 + §7.1 R2):

- total HTTP calls ≤ 16 (12 baseline + 4 burst);
- adjacent burst calls separated by ≥ 250 ms;
- ``--no-rate-limit-probe`` ⇒ ``enabled=False`` ⇒ ZERO HTTP traffic +
  ``probe_disabled=True`` + verdict unaffected;
- raw key never enters the result struct (PRD §6.2 A8 + §5.1 C2);
  every error message goes through ``transparent_log.redact_error``.

This is the modular distribution. ``audit.py`` mirrors the helpers +
the entry inside ``# === probe rate-limit ===`` / ``# === /probe rate-limit ===``;
the parity check lives in ``tests/test_dual_distribution_parity.py``.
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

from api_relay_audit.probe.types import ProbeError, RateLimitResult
from api_relay_audit.transparent_log import redact_error


# ---------------------------------------------------------------------------
# Constants — keep ASCII / character-stable for dual-dist parity
# ---------------------------------------------------------------------------

_RL_PATH = "/v1/chat/completions"
_RL_TIMEOUT_S = 8  # PRD §5.2 N1
_RL_MODEL = "gpt-4o-mini"

# Header-name vocabularies (lower-cased; classifier is case-insensitive).
_OPENAI_RL_HEADERS = frozenset({
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
})
_ANTHROPIC_RL_HEADERS = frozenset({
    "anthropic-ratelimit-requests-limit",
    "anthropic-ratelimit-requests-remaining",
    "anthropic-ratelimit-requests-reset",
    "anthropic-ratelimit-tokens-limit",
    "anthropic-ratelimit-tokens-remaining",
    "anthropic-ratelimit-tokens-reset",
    "anthropic-ratelimit-input-tokens-limit",
    "anthropic-ratelimit-input-tokens-remaining",
    "anthropic-ratelimit-output-tokens-limit",
    "anthropic-ratelimit-output-tokens-remaining",
})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def classify_compliance(headers_seen) -> str:
    """Reduce the set of observed header names to a compliance bucket.

    Rules (design §7 Story-5):

    * any OpenAI rate-limit header → ``openai`` (wins ties — OpenAI's
      contract is the more widely deployed one and most "anthropic"
      proxies also advertise ``x-ratelimit-*`` once they front through
      a LiteLLM-style adapter);
    * else any Anthropic rate-limit header → ``anthropic``;
    * else → ``absent`` (non-compliant relay).
    """
    lowered = {str(h).lower() for h in headers_seen}
    if lowered & _OPENAI_RL_HEADERS:
        return "openai"
    if lowered & _ANTHROPIC_RL_HEADERS:
        return "anthropic"
    return "absent"


def parse_retry_after(value) -> str:
    """Classify a ``Retry-After`` header value into a stable category.

    Returns ``"seconds"`` for a non-negative integer, ``"http_date"``
    for anything containing a clock (``:``) or ``GMT`` marker, and
    ``"unknown"`` for missing / unparseable input.
    """
    if value is None:
        return "unknown"
    s = str(value).strip()
    if not s:
        return "unknown"
    if s.isdigit():
        return "seconds"
    upper = s.upper()
    if "GMT" in upper or ":" in s:
        return "http_date"
    return "unknown"


def _detect_envelope(body: str) -> str:
    """Classify a 429 response body's envelope shape.

    Mirror of ``api_relay_audit.probe.auth_sniff.detect_envelope`` — kept
    local so this module has zero sibling-probe imports (design §4.5
    "modules don't import from sibling probe submodules").
    """
    if not body:
        return "absent"
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "absent"
    if not isinstance(data, dict):
        return "absent"
    if data.get("type") == "error" and isinstance(data.get("error"), dict):
        return "anthropic-style"
    err = data.get("error")
    if isinstance(err, dict):
        keys = set(err.keys())
        if "message" in keys and (keys & {"type", "code"}):
            return "openai-style"
        return "non-standard"
    if err is not None:
        return "non-standard"
    return "absent"


def _lower_headers(headers) -> dict:
    """Return a lower-cased ``dict[str,str]`` view of an HTTP header dict.

    Tolerates both ``dict[str,str]`` (most mocks) and ``httpx.Headers``
    (production). Missing / non-string values are skipped.
    """
    out = {}
    if not headers:
        return out
    try:
        items = headers.items()
    except AttributeError:
        return out
    for k, v in items:
        if isinstance(k, str):
            out[k.lower()] = "" if v is None else str(v)
    return out


def _extract_rpm(lower_headers: dict) -> Optional[int]:
    """Best-effort rpm from OpenAI then Anthropic header. Non-int → None."""
    for key in ("x-ratelimit-limit-requests", "anthropic-ratelimit-requests-limit"):
        raw = lower_headers.get(key)
        if raw is None:
            continue
        try:
            return int(str(raw).strip())
        except (ValueError, TypeError):
            return None
    return None


def _make_body() -> bytes:
    return json.dumps({
        "model": _RL_MODEL,
        "max_tokens": 1,
        "messages": [{"role": "user", "content": "."}],
    }).encode("utf-8")


# ---------------------------------------------------------------------------
# Main entry
# ---------------------------------------------------------------------------


def probe_rate_limit(
    client,
    *,
    enabled: bool = True,
    baseline: int = 12,
    burst: int = 4,
    min_gap_s: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
) -> RateLimitResult:
    """Run baseline + burst rate-limit probes against ``client``.

    Parameters
    ----------
    client
        ``APIClient``-shaped object. Must expose ``api_key`` (str) and
        ``raw_request(method, path, headers, body, content_type, timeout)``.
    enabled
        ``--no-rate-limit-probe`` ⇒ ``False``. Skips ALL HTTP traffic and
        returns ``probe_disabled=True`` (PRD §6.2 A7).
    baseline, burst
        Phase counts. PM-confirmed default is 12 + 4 (PRD §3.4 / §5.1 C4
        / §7.1 R2). Total HTTP calls are always ≤ ``baseline + burst``;
        a 429 short-circuits the run (PRD §6.2 A7).
    min_gap_s
        Minimum spacing between adjacent burst requests (PRD §7.1 R2,
        ≥ 0.25 s).
    sleep / now
        Injected ``time.sleep`` and ``time.monotonic`` analogues so unit
        tests can exercise the burst-gap logic without real wall-clock.

    Returns
    -------
    RateLimitResult
        Per design §4.1 + §7 Story-5. ``status='error'`` only when every
        single HTTP call transport-failed; partial failures are
        absorbed and the probe continues so we still get a compliance
        verdict.
    """
    if not enabled:
        return RateLimitResult(
            status="ok",
            probe_disabled=True,
            compliance="absent",
            signals=["rate_limit:probe_disabled"],
        )

    api_key = getattr(client, "api_key", None) or ""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    body = _make_body()

    headers_seen: set = set()
    rpm_first: Optional[int] = None
    triggered_429 = False
    samples_to_429: Optional[int] = None
    envelope_429 = "absent"
    retry_after_pattern = "unknown"
    burst_window_s: Optional[int] = None
    transport_error_count = 0
    first_transport_error: Optional[str] = None
    burst_start_t: Optional[float] = None
    burst_end_t: Optional[float] = None

    total_requests = baseline + burst
    last_call_t: Optional[float] = None

    for idx in range(total_requests):
        in_burst = idx >= baseline

        if in_burst and last_call_t is not None:
            elapsed = now() - last_call_t
            if elapsed < min_gap_s:
                sleep(max(0.0, min_gap_s - elapsed))

        call_t = now()
        if in_burst and burst_start_t is None:
            burst_start_t = call_t

        r = client.raw_request(
            method="POST",
            path=_RL_PATH,
            headers=headers,
            body=body,
            content_type="application/json",
            timeout=_RL_TIMEOUT_S,
        )
        last_call_t = now()
        if in_burst:
            burst_end_t = last_call_t

        status = r.get("status", 0) or 0
        if r.get("error"):
            transport_error_count += 1
            if first_transport_error is None:
                first_transport_error = r.get("error")

        lower = _lower_headers(r.get("headers"))
        for k in lower:
            headers_seen.add(k)

        if rpm_first is None:
            rpm_first = _extract_rpm(lower)

        if status == 429:
            triggered_429 = True
            samples_to_429 = idx + 1
            envelope_429 = _detect_envelope(r.get("body") or "")
            retry_after_pattern = parse_retry_after(lower.get("retry-after"))
            break

    if burst_start_t is not None and burst_end_t is not None:
        burst_window_s = int(round(burst_end_t - burst_start_t))

    compliance = classify_compliance(headers_seen)

    signals: list[str] = []
    if triggered_429:
        signals.append("rate_limit:triggered_429")
    signals.append(f"rate_limit:compliance_{compliance}")
    all_failed = transport_error_count >= total_requests
    if all_failed:
        signals.append("rate_limit:transport_error")

    error: Optional[ProbeError] = None
    status_field = "ok"
    if all_failed:
        status_field = "error"
        raw_msg = redact_error(str(first_transport_error)) or "transport_error"
        if api_key and api_key in raw_msg:
            raw_msg = raw_msg.replace(api_key, "<redacted>")
        error = ProbeError(
            code="transport_error:0",
            message=raw_msg,
        )

    return RateLimitResult(
        status=status_field,
        rpm_observed=rpm_first,
        headers_seen=sorted(headers_seen),
        envelope_429=envelope_429,
        retry_after_pattern=retry_after_pattern,
        burst_window_s=burst_window_s,
        triggered_429=triggered_429,
        samples_to_429=samples_to_429,
        compliance=compliance,
        probe_disabled=False,
        signals=signals,
        error=error,
    )


__all__ = [
    "classify_compliance",
    "parse_retry_after",
    "probe_rate_limit",
]
