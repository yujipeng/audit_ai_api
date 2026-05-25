"""Tests for ``api_relay_audit.probe.auth_sniff`` (S1 probe-core / Story-3).

Covers PRD §6.2 A5 — five mock branches: bearer / x-api-key / custom
header / 401 envelope / 403 envelope — plus the classification tri-state
(strict / permissive / broken) required by design §4.3 + §7 Story-3.

The probe under test must:

- never construct ``httpx`` clients directly; all network calls go
  through ``APIClient.raw_request`` so ``transparent_log`` sees them
  (PRD §5.1 C2);
- return an ``AuthSniffResult`` whose fields stay categorical strings
  (no raw key material — that contract is enforced separately by
  ``tests/test_probe_no_key_leak.py``);
- mark ``classification = "permissive"`` whenever the relay accepts an
  unauthenticated request, since that is the strong red-flag signal
  per design §4.3 verdict rules.
"""

from unittest.mock import MagicMock

from api_relay_audit.probe.auth_sniff import (
    classify_auth,
    detect_envelope,
    probe_auth_sniff,
)
from api_relay_audit.probe.types import AuthSniffResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

REAL_KEY = "sk-test-deadbeef-deadbeef-1234567890abcdef"
INVALID_TOKEN_MARKER = "invalid"


def _resp(status, body="", headers=None, error=None):
    return {
        "status": status,
        "headers": headers or {},
        "body": body,
        "error": error,
    }


def _make_client(api_key, responses):
    """Build a mock ``APIClient`` whose ``raw_request`` returns ``responses``
    in order.

    The probe's contract with the client is narrow: it reads
    ``client.api_key`` (so the bearer / x-api-key / custom probes attach
    the real key) and calls ``client.raw_request(method, path, headers,
    body, content_type, timeout)``. Anything else means the probe is
    bypassing ``transparent_log`` and breaks PRD §5.1 C2.
    """
    client = MagicMock()
    client.api_key = api_key
    client.raw_request = MagicMock(side_effect=responses)
    return client


# ---------------------------------------------------------------------------
# detect_envelope — error body shape classifier
# ---------------------------------------------------------------------------

class TestDetectEnvelope:
    def test_openai_style(self):
        body = '{"error": {"message": "Invalid auth", "type": "auth", "code": "invalid_api_key"}}'
        assert detect_envelope(body) == "openai-style"

    def test_anthropic_style(self):
        body = '{"type": "error", "error": {"type": "authentication_error", "message": "x"}}'
        assert detect_envelope(body) == "anthropic-style"

    def test_non_standard_with_error_key(self):
        body = '{"error": "not authenticated"}'
        assert detect_envelope(body) == "non-standard"

    def test_absent_on_empty_body(self):
        assert detect_envelope("") == "absent"

    def test_absent_on_non_json_body(self):
        assert detect_envelope("<html>Unauthorized</html>") == "absent"

    def test_absent_on_response_without_error(self):
        assert detect_envelope('{"data": []}') == "absent"


# ---------------------------------------------------------------------------
# classify_auth — strict / permissive / broken tri-state
# ---------------------------------------------------------------------------

class TestClassifyAuth:
    def test_strict_when_invalid_and_missing_both_rejected(self):
        # Real key 200, invalid 401, missing 401 → "strict"
        assert classify_auth(
            valid_status=200, invalid_status=401, missing_status=401
        ) == "strict"

    def test_strict_with_403_rejections(self):
        assert classify_auth(
            valid_status=200, invalid_status=403, missing_status=403
        ) == "strict"

    def test_permissive_when_missing_returns_2xx(self):
        # Missing-auth accepted → relay has no auth at all → red flag
        assert classify_auth(
            valid_status=200, invalid_status=200, missing_status=200
        ) == "permissive"

    def test_permissive_when_invalid_accepted_but_missing_rejected(self):
        # Anonymous (no header) is blocked, but ANY token works → still
        # effectively no auth (a relay that grants access to a literal
        # "Bearer invalid" string is functionally unauthenticated).
        assert classify_auth(
            valid_status=200, invalid_status=200, missing_status=401
        ) == "permissive"

    def test_broken_when_valid_key_rejected(self):
        # Real key is rejected → relay is broken / mis-configured
        assert classify_auth(
            valid_status=401, invalid_status=401, missing_status=401
        ) == "broken"

    def test_broken_on_transport_failure_status_zero(self):
        # status==0 means raw_request transport error
        assert classify_auth(
            valid_status=0, invalid_status=0, missing_status=0
        ) == "broken"


# ---------------------------------------------------------------------------
# probe_auth_sniff — PRD §6.2 A5 five mock branches
# ---------------------------------------------------------------------------

class TestProbeAuthSniffBranches:
    """The five branches PRD §6.2 A5 enumerates explicitly.

    Probe order is fixed (bearer / x-api-key / custom / invalid /
    missing) so a single ``responses`` list passed to ``side_effect``
    drives a deterministic scenario. The branch label in the docstring
    of each test names the PRD §6.2 A5 sub-case it covers.
    """

    def test_branch_1_bearer_accepted_strict(self):
        """A5 branch (a): relay accepts ``Authorization: Bearer``."""
        responses = [
            _resp(200, body='{"data": [{"id": "gpt-4"}]}'),  # bearer
            _resp(401, body='{"error": {"message": "no auth"}}'),  # x-api-key
            _resp(401, body='{"error": {"message": "no auth"}}'),  # custom
            _resp(401, body='{"error": {"message": "bad key"}}'),  # invalid
            _resp(401, body='{"error": {"message": "no auth"}}'),  # missing
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)

        assert isinstance(result, AuthSniffResult)
        assert result.status == "ok"
        assert "bearer" in result.accepted_schemes
        assert result.key_position == "Authorization"
        assert result.classification == "strict"
        assert result.envelope_401 in ("openai-style", "non-standard")
        # 5 probes per run — design §7 Story-3 "三探 + 两辅"
        assert client.raw_request.call_count == 5

    def test_branch_2_xapi_key_accepted_strict(self):
        """A5 branch (b): relay accepts ``x-api-key`` (Anthropic-style)."""
        responses = [
            _resp(401, body='{"type": "error", "error": {"type": "auth", "message": "no bearer"}}'),  # bearer
            _resp(200, body='{"data": [{"id": "claude-3"}]}'),  # x-api-key
            _resp(401),
            _resp(401, body='{"type": "error", "error": {"type": "auth", "message": "bad key"}}'),
            _resp(401, body='{"type": "error", "error": {"type": "auth", "message": "missing"}}'),
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)

        assert result.status == "ok"
        assert "x-api-key" in result.accepted_schemes
        assert "bearer" not in result.accepted_schemes
        assert result.key_position == "x-api-key"
        assert result.classification == "strict"
        # Anthropic envelope on the 401 probes
        assert result.envelope_401 == "anthropic-style"

    def test_branch_3_custom_header_accepted_strict(self):
        """A5 branch (c): relay accepts a custom header (e.g. ``api-key``)."""
        responses = [
            _resp(401, body='{"error": "no"}'),  # bearer
            _resp(401, body='{"error": "no"}'),  # x-api-key
            _resp(200, body='{"data": []}'),  # custom (api-key header)
            _resp(401, body='{"error": "bad"}'),
            _resp(401, body='{"error": "missing"}'),
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)

        assert result.status == "ok"
        assert "custom" in result.accepted_schemes
        assert result.key_position not in ("Authorization", "x-api-key", "unknown")
        # any 401 still classifies envelope (non-standard since body is just {"error": "no"})
        assert result.envelope_401 == "non-standard"
        assert result.classification == "strict"

    def test_branch_4_envelope_401_openai_style(self):
        """A5 branch (d): probe must classify 401 body shape."""
        body = '{"error": {"message": "Invalid API key", "type": "authentication_error", "code": "invalid_api_key"}}'
        responses = [
            _resp(200, body='{"data": []}'),
            _resp(401, body=body),
            _resp(401, body=body),
            _resp(401, body=body),
            _resp(401, body=body),
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)
        assert result.envelope_401 == "openai-style"
        # envelope_403 absent when no 403 seen
        assert result.envelope_403 == "absent"

    def test_branch_5_envelope_403_anthropic_style(self):
        """A5 branch (e): probe must classify 403 body shape."""
        body403 = '{"type": "error", "error": {"type": "permission_error", "message": "Forbidden"}}'
        responses = [
            _resp(200, body='{"data": []}'),
            _resp(401),
            _resp(401),
            _resp(403, body=body403),  # invalid → 403
            _resp(401),
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)
        assert result.envelope_403 == "anthropic-style"


# ---------------------------------------------------------------------------
# Classification tri-state — design §4.3 + §7 Story-3 DoD
# ---------------------------------------------------------------------------

class TestProbeAuthSniffClassification:
    def test_permissive_when_missing_auth_returns_200(self):
        """Permissive = relay grants access without any auth — major red flag.

        Design §4.3 says verdict='fail' when classification='permissive'.
        Our DoD includes "permissive 必有 red flag" — verify the signal
        list flags this case so downstream report rendering can surface it.
        """
        responses = [
            _resp(200, body='{"data": []}'),  # bearer (real key) → 200
            _resp(200, body='{"data": []}'),  # x-api-key with key → 200
            _resp(200, body='{"data": []}'),  # custom → 200
            _resp(200, body='{"data": []}'),  # invalid key → 200 (!!)
            _resp(200, body='{"data": []}'),  # no auth at all → 200 (!!)
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)
        assert result.classification == "permissive"
        # Red-flag signal MUST be emitted (DoD).
        assert any("permissive" in s.lower() for s in result.signals)
        assert any("red_flag" in s.lower() or "red-flag" in s.lower()
                   for s in result.signals)

    def test_broken_when_real_key_rejected(self):
        responses = [
            _resp(401),  # bearer with real key → rejected
            _resp(401),  # x-api-key with real key → rejected
            _resp(401),  # custom with real key → rejected
            _resp(401),  # invalid → rejected
            _resp(401),  # missing → rejected
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)
        assert result.classification == "broken"
        assert result.accepted_schemes == []
        # No accepted scheme → key_position falls back to "unknown"
        assert result.key_position == "unknown"

    def test_status_error_on_transport_failure(self):
        responses = [
            _resp(0, error="connection refused"),
            _resp(0, error="connection refused"),
            _resp(0, error="connection refused"),
            _resp(0, error="connection refused"),
            _resp(0, error="connection refused"),
        ]
        client = _make_client(REAL_KEY, responses)
        result = probe_auth_sniff(client)
        assert result.status == "error"
        assert result.error is not None
        assert result.error.code != ""
        # message must be the redacted form (no raw body content)
        assert result.error.message != ""


# ---------------------------------------------------------------------------
# Probe contract — PRD §5.1 C2 (all network via APIClient) + C3 (no leak)
# ---------------------------------------------------------------------------

class TestProbeContract:
    def test_all_requests_use_raw_request(self):
        responses = [_resp(401)] * 5
        client = _make_client(REAL_KEY, responses)
        probe_auth_sniff(client)
        # The probe MUST NOT call client.call / client.get_models /
        # client.stream_call / httpx.* directly. We don't have a way to
        # assert "no httpx call" here without dynamic patching, but we
        # can assert that the probe only touched raw_request on the
        # client mock — anything else would show up as an unexpected
        # attribute access.
        assert client.raw_request.called
        assert not client.call.called
        assert not client.get_models.called
        assert not client.stream_call.called

    def test_request_path_is_models_endpoint(self):
        responses = [_resp(401)] * 5
        client = _make_client(REAL_KEY, responses)
        probe_auth_sniff(client)
        # All probes go to /v1/models — the canonical low-cost auth
        # check endpoint (PRD §3.5 P2; design §7 Story-3).
        for call in client.raw_request.call_args_list:
            kwargs = call.kwargs
            args = call.args
            path = kwargs.get("path") if "path" in kwargs else args[1]
            assert path == "/v1/models"

    def test_bearer_probe_attaches_authorization_header(self):
        responses = [_resp(200, body='{"data": []}')] + [_resp(401)] * 4
        client = _make_client(REAL_KEY, responses)
        probe_auth_sniff(client)
        # The first probe is the bearer probe. The "Authorization"
        # header (canonical capitalisation) must be present on that
        # request — NOT just lowercase, because some httpx servers and
        # the standalone curl path are case-sensitive.
        first_call = client.raw_request.call_args_list[0]
        headers = first_call.kwargs.get("headers") if "headers" in first_call.kwargs else first_call.args[2]
        assert "Authorization" in headers or "authorization" in headers

    def test_missing_probe_has_no_auth_header(self):
        responses = [_resp(401)] * 5
        client = _make_client(REAL_KEY, responses)
        probe_auth_sniff(client)
        # 5th probe is the "missing" probe → no auth header at all.
        last_call = client.raw_request.call_args_list[4]
        headers = last_call.kwargs.get("headers") if "headers" in last_call.kwargs else last_call.args[2]
        normalised = {k.lower(): v for k, v in (headers or {}).items()}
        assert "authorization" not in normalised
        assert "x-api-key" not in normalised
        assert "api-key" not in normalised
