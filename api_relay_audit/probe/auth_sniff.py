"""P2 auth-sniff probe (S1 probe-core / Story-3, TES-151).

Sniffs the relay's authentication shape with five low-cost ``GET
/v1/models`` probes and classifies the result tri-state per design
§4.3 + §7 Story-3:

* ``strict`` — relay accepts a real key under at least one scheme and
  rejects both an obviously-invalid token and an unauthenticated
  request;
* ``permissive`` — relay grants access without a real key (either
  responds 2xx to an invalid token, or responds 2xx to no token at
  all). This is the strong red-flag signal that drives
  ``ProbeReport.verdict='fail'`` (design §4.3);
* ``broken`` — relay rejects the real key, transport fails, or
  responds with a non-2xx everywhere we tried.

PRD §6.2 A5 enumerates the five mock branches that
``tests/test_probe_auth_sniff.py`` exercises: bearer / x-api-key /
custom header / 401 / 403. PRD §6.2 A8 + §5.1 C2 forbid raw key
material from leaking into ``ProbeReport`` JSON or stdout — every
``error.message`` is routed through
``transparent_log.redact_error`` and only categorical strings make it
into the result struct.

All HTTP traffic goes through ``APIClient.raw_request`` so
``transparent_log`` records SHA-256 hashes of every probe (PRD §5.1
C2). The probe never touches ``httpx`` directly.
"""

from __future__ import annotations

import json

from api_relay_audit.probe.types import AuthSniffResult, ProbeError
from api_relay_audit.transparent_log import redact_error


# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
#
# We probe ``/v1/models`` on every branch. It's the canonical
# unauthenticated-vs-authenticated surface every relay exposes (PRD
# §3.5), zero-cost (no token spend), and standard across OpenAI- and
# Anthropic-shaped relays. PRD §5.1 C3 requires probe traffic to stay
# on the user's ``base_url`` — ``raw_request`` already enforces this
# through ``client.base_url`` so we never construct host URLs locally.
_PROBE_PATH = "/v1/models"
_PROBE_TIMEOUT_S = 8  # PRD §5.2 N1
_INVALID_TOKEN = "INVALID-TOKEN-FOR-AUTH-SNIFF-PROBE"  # never confused with a real key

# Custom-header schemes some relays accept in the wild. ``api-key``
# (Azure-style) is the most common; the rest are observed on private
# forks of one-api / new-api.
_CUSTOM_HEADER_NAME = "api-key"

# Five probes in fixed order. Position is part of the contract: the
# tests assert on call indices, and the dual-distribution Section in
# ``audit.py`` mirrors this order character-strict.
_PROBE_ORDER = ("bearer", "x-api-key", "custom", "invalid", "missing")


# ----------------------------------------------------------------------
# Envelope detection
# ----------------------------------------------------------------------

def detect_envelope(body: str) -> str:
    """Classify a 4xx response body shape.

    Returns one of ``"openai-style"``, ``"anthropic-style"``,
    ``"non-standard"``, or ``"absent"``.

    OpenAI:    ``{"error": {"message": "...", "type": "...", "code": "..."}}``
    Anthropic: ``{"type": "error", "error": {"type": "...", "message": "..."}}``

    The classifier is tolerant of extra fields and of the body being
    a non-JSON string (in which case it's ``"absent"``).
    """
    if not body:
        return "absent"
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return "absent"
    if not isinstance(data, dict):
        return "absent"

    # Anthropic: top-level ``type=="error"`` + nested ``error`` object.
    if data.get("type") == "error" and isinstance(data.get("error"), dict):
        return "anthropic-style"

    err = data.get("error")
    if isinstance(err, dict):
        # OpenAI shape requires at least ``message`` and ``type``/``code``.
        keys = set(err.keys())
        if "message" in keys and (keys & {"type", "code"}):
            return "openai-style"
        return "non-standard"
    if err is not None:
        # ``"error": "string"`` is the loose non-standard envelope.
        return "non-standard"
    return "absent"


# ----------------------------------------------------------------------
# Classification
# ----------------------------------------------------------------------

def classify_auth(valid_status: int, invalid_status: int,
                  missing_status: int) -> str:
    """Reduce three response codes to ``strict | permissive | broken``.

    Inputs are the HTTP status codes (or ``0`` on transport failure)
    of the *most permissive* successful real-key probe, the invalid
    token probe, and the missing-auth probe respectively.

    Rules (design §4.3):

    * any 2xx on a no-auth or invalid-token probe → ``permissive``
      (relay grants access without a real key — verdict='fail');
    * real key never returns 2xx → ``broken`` (relay or transport
      mis-configured);
    * otherwise → ``strict``.

    The ``permissive`` check runs FIRST: if the real-key probe also
    happens to 2xx (which it usually does), we still want the
    permissive verdict because that's the dominant security signal.
    """
    def _ok(s):
        return 200 <= s < 300

    # Strong red flag: relay accepts a request with no auth header at
    # all, or accepts a literal "Bearer INVALID-TOKEN..." string. Both
    # mean the relay isn't really authenticating anything.
    if _ok(missing_status) or _ok(invalid_status):
        return "permissive"

    # Real key never gets accepted → broken.
    if not _ok(valid_status):
        return "broken"

    return "strict"


# ----------------------------------------------------------------------
# Probe entry point
# ----------------------------------------------------------------------

def _build_headers(scheme: str, key: str) -> dict:
    """Return the auth headers for a single probe scheme."""
    if scheme == "bearer":
        return {"Authorization": f"Bearer {key}"}
    if scheme == "x-api-key":
        return {"x-api-key": key}
    if scheme == "custom":
        return {_CUSTOM_HEADER_NAME: key}
    if scheme == "invalid":
        return {"Authorization": f"Bearer {_INVALID_TOKEN}"}
    if scheme == "missing":
        return {}
    raise ValueError(f"unknown scheme: {scheme}")


def _key_position_for(scheme: str) -> str:
    """Map a probe scheme to its ``key_position`` label."""
    if scheme == "bearer":
        return "Authorization"
    if scheme == "x-api-key":
        return "x-api-key"
    if scheme == "custom":
        return _CUSTOM_HEADER_NAME
    return "unknown"


def probe_auth_sniff(client) -> AuthSniffResult:
    """Run the five auth probes against ``client.base_url`` and classify.

    Args:
        client: An ``APIClient`` instance. The probe reads
            ``client.api_key`` and calls ``client.raw_request``; it
            never constructs ``httpx`` directly (PRD §5.1 C2).

    Returns:
        ``AuthSniffResult`` populated per design §4.1. Errors never
        raise — transport failures show up as ``status="error"`` with a
        redacted ``ProbeError``.
    """
    api_key = getattr(client, "api_key", None) or ""

    statuses = {}
    bodies = {}
    transport_errors = []
    for scheme in _PROBE_ORDER:
        headers = _build_headers(scheme, api_key)
        r = client.raw_request(
            method="GET",
            path=_PROBE_PATH,
            headers=headers,
            body=b"",
            content_type="application/json",
            timeout=_PROBE_TIMEOUT_S,
        )
        statuses[scheme] = r.get("status", 0) or 0
        bodies[scheme] = r.get("body", "") or ""
        if r.get("error"):
            transport_errors.append((scheme, r.get("error")))

    # Accepted schemes = the real-key probes that returned 2xx. Order
    # matches _PROBE_ORDER so downstream report rendering is stable.
    accepted_schemes = []
    for scheme in ("bearer", "x-api-key", "custom"):
        if 200 <= statuses[scheme] < 300:
            accepted_schemes.append(scheme)

    # ``valid_status`` for classification = the *best* real-key result.
    # If any real-key probe succeeded we treat that as the valid path.
    real_key_statuses = [statuses[s] for s in ("bearer", "x-api-key", "custom")]
    valid_status = next(
        (s for s in real_key_statuses if 200 <= s < 300),
        max(real_key_statuses) if real_key_statuses else 0,
    )

    classification = classify_auth(
        valid_status=valid_status,
        invalid_status=statuses["invalid"],
        missing_status=statuses["missing"],
    )

    # Envelope detection — first body with the matching status code wins.
    envelope_401 = "absent"
    envelope_403 = "absent"
    for scheme, status in statuses.items():
        body = bodies[scheme]
        if status == 401 and envelope_401 == "absent":
            envelope_401 = detect_envelope(body)
        if status == 403 and envelope_403 == "absent":
            envelope_403 = detect_envelope(body)

    # ``key_position`` reports the FIRST accepted scheme's header,
    # falling back to "unknown" if no real-key probe was accepted.
    key_position = "unknown"
    if accepted_schemes:
        key_position = _key_position_for(accepted_schemes[0])

    # Signals — bullet markers consumed by report-generator.
    signals = []
    if classification == "permissive":
        signals.append("auth:permissive")
        signals.append("auth:red_flag_no_real_authentication")
    if classification == "broken":
        signals.append("auth:broken")
    if 200 <= statuses["missing"] < 300:
        signals.append("auth:missing_auth_returns_2xx")
    if 200 <= statuses["invalid"] < 300:
        signals.append("auth:invalid_token_returns_2xx")

    # Status — error iff every probe transport-failed; otherwise ok.
    if transport_errors and len(transport_errors) == len(_PROBE_ORDER):
        # Use the first transport error as representative; the message
        # is already-redacted per PRD §5.1 C2 (raw_request hands us
        # back ``error`` as a short diagnostic, and we redact again
        # defensively here in case future raw_request paths change).
        first_scheme, first_err = transport_errors[0]
        return AuthSniffResult(
            status="error",
            accepted_schemes=[],
            envelope_401="absent",
            envelope_403="absent",
            key_position="unknown",
            classification="broken",
            signals=["auth:transport_error"],
            error=ProbeError(
                code=f"transport_error:{first_scheme}",
                message=redact_error(str(first_err)) or "transport_error",
            ),
        )

    return AuthSniffResult(
        status="ok",
        accepted_schemes=accepted_schemes,
        envelope_401=envelope_401,
        envelope_403=envelope_403,
        key_position=key_position,
        classification=classification,
        signals=signals,
        error=None,
    )


__all__ = [
    "classify_auth",
    "detect_envelope",
    "probe_auth_sniff",
]
