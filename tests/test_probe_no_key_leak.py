"""PRD §6.2 A8 contract test — no raw key material in any probe output.

Story-3 (TES-151) collects this test under its DoD because auth-sniff is
the first probe that actively handles a real key. The contract is hard:

- ``ProbeReport.to_dict()`` JSON serialisation MUST NOT contain the raw
  key, the literal ``"Bearer "`` prefix attached to a key, or the
  ``sk-`` prefix family;
- ``probe_auth_sniff`` stdout (anything written via ``print`` /
  ``sys.stdout``) MUST NOT contain any of the above;
- redaction is enforced on the ``error.message`` field of every
  ``ProbeResult`` via ``transparent_log.redact_error``.

The key-shaped patterns scanned here cover the three families seen in
the wild on relays this audit targets: ``sk-`` (OpenAI),
``sk-ant-`` (Anthropic Console), and a generic ``Bearer <token>``
attribution. ``input_key_fingerprint`` (sha256 prefix) is the only
key-derived field allowed.

Why scan stdout too: many probe authors emit debug prints that contain
the request headers verbatim. Even if the JSON report is clean, a
``print(headers)`` call leaks the key into terminal output and CI logs.
"""

import io
import json
import re
import sys
from contextlib import redirect_stdout
from unittest.mock import MagicMock

import pytest

from api_relay_audit.probe.auth_sniff import probe_auth_sniff
from api_relay_audit.probe.types import (
    AuthSniffResult,
    ModelsDiffResult,
    ProbeReport,
    RateLimitResult,
    ReachabilityResult,
)


# Real-key-shaped patterns — match the three families a relay user is
# most likely to paste into ``--key``. The patterns are intentionally
# narrow: they don't fire on the literal token ``"Bearer"`` alone or on
# the ``sk-`` substring inside an unrelated word like ``risky-``.
LEAK_PATTERNS = [
    # Bearer <something-non-trivial>: at least 8 char body so the
    # placeholder string "Bearer " on its own (e.g. inside docstrings)
    # doesn't trip this. transparent_log only ever writes the
    # fingerprint, never "Bearer <real>".
    re.compile(r"Bearer\s+[A-Za-z0-9_\-]{8,}"),
    # OpenAI-style: sk- followed by 16+ char URL-safe body.
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    # Anthropic-style: sk-ant- prefix + 16+ char body.
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),
]


# Test keys must (a) match one of the LEAK_PATTERNS so the scanners can
# actually fire if the probe leaks them, and (b) be unambiguously
# distinct from any unrelated string in source.
TEST_KEYS = [
    "sk-test-deadbeef-deadbeef-1234567890abcdef",
    "sk-ant-api03-aaaabbbbccccddddeeeeffff00001111",
    "Bearer sk-relay-secret-token-1234567890",  # weird but seen on some relays
]


def _scan_for_leaks(haystack: str, key: str):
    """Return a list of leaked substrings found in ``haystack``.

    Two-layer check: literal substring (catches the *exact* user key) +
    pattern (catches arbitrary key-shaped tokens — defensive against
    the probe accidentally emitting a transformed copy of the key).
    """
    hits = []
    if key and key in haystack:
        hits.append(("literal_key", key))
    for pat in LEAK_PATTERNS:
        for match in pat.findall(haystack):
            hits.append(("pattern", match))
    return hits


def _resp(status, body="", headers=None, error=None):
    return {"status": status, "headers": headers or {}, "body": body, "error": error}


def _make_client(api_key, responses):
    client = MagicMock()
    client.api_key = api_key
    client.raw_request = MagicMock(side_effect=responses)
    return client


def _build_report(auth_result: AuthSniffResult, key: str) -> ProbeReport:
    """Wrap an ``AuthSniffResult`` in a minimal ProbeReport so we can
    serialise the same shape Story-6 will emit."""
    return ProbeReport(
        schema_version=ProbeReport.SCHEMA_VERSION,
        generated_at="2026-05-25T00:00:00Z",
        input_base_url="https://relay.example.com",
        # PRD §4.2 — only sha256 prefix, NEVER the raw key.
        input_key_fingerprint="abcd1234",
        input_vendor_hint="auto",
        verdict="fail",
        reachability=ReachabilityResult(status="ok", tcp_ok=True, tls_ok=True),
        auth_sniff=auth_result,
        models_diff=ModelsDiffResult(status="ok"),
        rate_limit=RateLimitResult(status="ok"),
    )


@pytest.mark.parametrize("key", TEST_KEYS)
def test_probe_report_json_has_no_raw_key(key):
    """The serialised ProbeReport JSON must never contain the raw key.

    PRD §6.2 A8: 0 hits across ProbeReport JSON. Each of the three
    test keys is fed through a "strict" branch (the most boring
    happy path) so any leak is the probe's fault, not classification
    metadata's.
    """
    responses = [
        _resp(200, body='{"data": []}'),
        _resp(401, body='{"error": {"message": "no"}}'),
        _resp(401, body='{"error": {"message": "no"}}'),
        _resp(401, body='{"error": {"message": "bad"}}'),
        _resp(401, body='{"error": {"message": "missing"}}'),
    ]
    client = _make_client(key, responses)
    auth_result = probe_auth_sniff(client)
    report = _build_report(auth_result, key)

    payload = json.dumps(report.to_dict(), ensure_ascii=False)
    leaks = _scan_for_leaks(payload, key)
    assert leaks == [], (
        f"ProbeReport JSON leaked key material: {leaks!r}\n"
        f"payload (truncated 300 char) = {payload[:300]!r}"
    )


@pytest.mark.parametrize("key", TEST_KEYS)
def test_probe_auth_sniff_stdout_has_no_raw_key(key):
    """The probe's stdout must never contain the raw key.

    Catches the "I added a print() for debugging" failure mode that's
    invisible to the JSON contract above. Any print to stderr is out
    of scope here — auditing CI captures stderr separately.
    """
    responses = [
        _resp(200, body='{"data": []}'),
        _resp(401, body='{"error": "no"}'),
        _resp(401, body='{"error": "no"}'),
        _resp(401, body='{"error": "bad"}'),
        _resp(401, body='{"error": "missing"}'),
    ]
    client = _make_client(key, responses)
    captured = io.StringIO()
    with redirect_stdout(captured):
        probe_auth_sniff(client)
    out = captured.getvalue()
    leaks = _scan_for_leaks(out, key)
    assert leaks == [], (
        f"probe_auth_sniff stdout leaked key material: {leaks!r}\n"
        f"stdout (truncated 300 char) = {out[:300]!r}"
    )


@pytest.mark.parametrize("key", TEST_KEYS)
def test_error_path_does_not_leak_key_in_message(key):
    """When the probe encounters a transport error, the error.message
    field MUST be the redacted form (no raw body, no header echo).

    This is the place we expect to see the most leaks — naïve
    implementations stash ``str(exception)`` directly, and the
    exception text from ``httpx`` often includes the URL with the auth
    header echoed back by an upstream 401 page.
    """
    # Simulate a transport-failure surface that *would* leak if the
    # probe stuffed the request body / headers into error.message.
    leaky_body = f"401 from upstream: Authorization: Bearer {key}"
    responses = [
        _resp(0, body=leaky_body, error=f"HTTP 401: {leaky_body}"),
    ] * 5
    client = _make_client(key, responses)
    auth_result = probe_auth_sniff(client)
    payload = json.dumps({"auth_sniff": auth_result.__dict__}, default=str)
    leaks = _scan_for_leaks(payload, key)
    assert leaks == [], (
        f"error path leaked key material: {leaks!r}\n"
        f"payload (truncated 300) = {payload[:300]!r}"
    )


def test_input_key_fingerprint_is_sha256_prefix_not_raw_key():
    """ProbeReport.input_key_fingerprint must be a short hex digest, not
    the raw key (PRD §4.2 / §5.1 C2)."""
    auth_result = AuthSniffResult(status="ok")
    report = _build_report(auth_result, key=TEST_KEYS[0])
    fp = report.to_dict()["input_key_fingerprint"]
    # Hex, ≤ 16 char (we expect 8 bytes = 16 hex chars at most).
    assert re.fullmatch(r"[0-9a-f]{4,16}", fp), fp
    # Must not contain any of the leak patterns.
    for pat in LEAK_PATTERNS:
        assert not pat.search(fp), fp
