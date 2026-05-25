"""Tests for ``api_relay_audit.probe.rate_limit_fp`` (S1 probe-core / Story-5).

Covers PRD §6.2 A7 — ``max_requests ≤ 16`` + ``--no-rate-limit-probe`` skip
(`probe_disabled=True`, verdict not driven to fail) — plus the three
compliance buckets (``openai`` / ``anthropic`` / ``absent``) from
design §7 Story-5 and the ``triggered_429`` short-circuit (no retry).

Mocking style: ``client.raw_request`` returns a scripted list of
``{"status", "headers", "body", "error"}`` dicts. Time and sleep are
injected so the burst-gap (R2 ≥ 250ms) is asserted without sleeping
real wall-clock seconds during CI.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from api_relay_audit.probe import rate_limit_fp as rlfp
from api_relay_audit.probe.types import RateLimitResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


REAL_KEY = "sk-test-deadbeef-deadbeef-1234567890abcdef"


def _resp(status=200, headers=None, body="", error=None):
    return {
        "status": status,
        "headers": headers or {},
        "body": body,
        "error": error,
    }


def _client(responses, api_key=REAL_KEY):
    """Build a mock ``APIClient`` whose ``raw_request`` returns ``responses``
    in order. The probe's contract is narrow: read ``client.api_key`` and
    call ``client.raw_request(method, path, headers, body, content_type,
    timeout)`` — anything else means it's bypassing ``transparent_log``.
    """
    c = MagicMock()
    c.api_key = api_key
    c.raw_request = MagicMock(side_effect=list(responses))
    return c


class _FakeClock:
    """Monotonic clock + ``sleep`` recorder for burst-gap assertions.

    Each call to ``now()`` returns ``current``; each call to ``sleep(s)``
    advances ``current`` by ``s`` and appends ``s`` to ``calls`` so the
    test can assert burst gaps are ≥ R2.
    """

    def __init__(self, start: float = 0.0):
        self.current = float(start)
        self.calls: list[float] = []

    def now(self) -> float:
        return self.current

    def sleep(self, seconds: float) -> None:
        self.calls.append(float(seconds))
        self.current += float(seconds)


OPENAI_HEADERS = {
    "x-ratelimit-limit-requests": "60",
    "x-ratelimit-remaining-requests": "59",
    "x-ratelimit-reset-requests": "1s",
}
ANTHROPIC_HEADERS = {
    "anthropic-ratelimit-requests-limit": "50",
    "anthropic-ratelimit-requests-remaining": "49",
    "anthropic-ratelimit-tokens-limit": "40000",
}


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_probe_rate_limit_is_callable(self):
        assert callable(rlfp.probe_rate_limit)

    def test_returns_rate_limit_result(self):
        result = rlfp.probe_rate_limit(_client([_resp()] * 16), enabled=True)
        assert isinstance(result, RateLimitResult)


# ---------------------------------------------------------------------------
# Compliance classifier
# ---------------------------------------------------------------------------


class TestClassifyCompliance:
    def test_openai_when_only_openai_headers_seen(self):
        seen = {"x-ratelimit-limit-requests"}
        assert rlfp.classify_compliance(seen) == "openai"

    def test_anthropic_when_only_anthropic_headers_seen(self):
        seen = {"anthropic-ratelimit-requests-limit"}
        assert rlfp.classify_compliance(seen) == "anthropic"

    def test_absent_when_no_known_headers(self):
        assert rlfp.classify_compliance(set()) == "absent"
        assert rlfp.classify_compliance({"server", "date"}) == "absent"

    def test_openai_wins_when_both_families_present(self):
        seen = {
            "x-ratelimit-limit-requests",
            "anthropic-ratelimit-requests-limit",
        }
        assert rlfp.classify_compliance(seen) == "openai"

    def test_case_insensitive_match(self):
        seen = {"X-RateLimit-Limit-Requests"}
        assert rlfp.classify_compliance(seen) == "openai"


# ---------------------------------------------------------------------------
# Retry-After parser
# ---------------------------------------------------------------------------


class TestParseRetryAfter:
    def test_seconds_for_pure_integer(self):
        assert rlfp.parse_retry_after("30") == "seconds"
        assert rlfp.parse_retry_after("0") == "seconds"
        assert rlfp.parse_retry_after(" 12 ") == "seconds"

    def test_http_date_when_contains_gmt(self):
        assert (
            rlfp.parse_retry_after("Wed, 21 Oct 2025 07:28:00 GMT")
            == "http_date"
        )

    def test_http_date_when_contains_colon_time(self):
        assert rlfp.parse_retry_after("21 Oct 2025 07:28:00") == "http_date"

    def test_unknown_when_missing(self):
        assert rlfp.parse_retry_after(None) == "unknown"
        assert rlfp.parse_retry_after("") == "unknown"

    def test_unknown_on_garbage(self):
        assert rlfp.parse_retry_after("soon") == "unknown"


# ---------------------------------------------------------------------------
# --no-rate-limit-probe skip behavior (PRD §6.2 A7)
# ---------------------------------------------------------------------------


class TestProbeDisabled:
    def test_disabled_skips_all_http_calls(self):
        c = _client([])  # would raise StopIteration on any call
        result = rlfp.probe_rate_limit(c, enabled=False)
        assert c.raw_request.call_count == 0
        assert result.probe_disabled is True

    def test_disabled_sets_status_ok_and_no_429(self):
        c = _client([])
        result = rlfp.probe_rate_limit(c, enabled=False)
        assert result.status == "ok"
        assert result.triggered_429 is False
        assert result.error is None

    def test_disabled_signals_marker(self):
        result = rlfp.probe_rate_limit(_client([]), enabled=False)
        assert "rate_limit:probe_disabled" in result.signals

    def test_disabled_compliance_absent(self):
        result = rlfp.probe_rate_limit(_client([]), enabled=False)
        assert result.compliance == "absent"


# ---------------------------------------------------------------------------
# max_requests ≤ 16 (PRD §5.1 C4 + §6.2 A7)
# ---------------------------------------------------------------------------


class TestMaxRequests:
    def test_total_http_calls_capped_at_16_when_all_ok(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert c.raw_request.call_count == 16  # 12 baseline + 4 burst
        assert result.status == "ok"
        assert result.triggered_429 is False

    def test_baseline_count_is_12_when_no_429(self):
        # If baseline runs full 12 → first burst call is the 13th overall.
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        rlfp.probe_rate_limit(c, enabled=True, sleep=clock.sleep, now=clock.now)
        # At minimum 12 calls happened; verify exact total is 16
        assert c.raw_request.call_count == 16


# ---------------------------------------------------------------------------
# Burst gap ≥ 250ms (PRD §7.1 R2)
# ---------------------------------------------------------------------------


class TestBurstGap:
    def test_sleep_called_with_min_250ms_between_burst_requests(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        rlfp.probe_rate_limit(
            c,
            enabled=True,
            min_gap_s=0.25,
            sleep=clock.sleep,
            now=clock.now,
        )
        # Among sleeps, each one must be >= 0 and meaningfully spaced.
        # The probe must sleep at least 3 times between the 4 burst
        # requests (n-1 gaps for n requests), each ≥ 0 (the gap may be
        # 0 if now() already advanced enough between adjacent calls in
        # the test, but here clock doesn't auto-advance so every gap
        # comes from sleep).
        # Sum of all sleep durations during burst ≥ 3 * 0.25 = 0.75 s.
        # Allow extra sleeps if the implementation pre-paces baseline
        # too, but require the burst window covers at least 0.75 s of
        # injected sleep.
        total = sum(clock.calls)
        assert total >= 0.75, f"injected sleep total {total} < 0.75 s"

    def test_no_real_time_sleep_in_unit_test(self):
        # Sanity: the test must use the injected clock — if the probe
        # falls back to time.sleep we'd hang here.
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        # Should complete near-instantly with injected sleep.
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.status == "ok"


# ---------------------------------------------------------------------------
# triggered_429 short-circuit (PRD §6.2 A7)
# ---------------------------------------------------------------------------


class TestTriggered429:
    def test_baseline_429_stops_immediately(self):
        # 4 OK then 1 429; remaining responses must NEVER be consumed.
        responses = [_resp(200, OPENAI_HEADERS)] * 4 + [
            _resp(429, {"retry-after": "30"}, body='{"error":{"message":"too fast","type":"rate_limit_error"}}')
        ] + [_resp(200, OPENAI_HEADERS)] * 11  # filler that should NOT run
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert c.raw_request.call_count == 5
        assert result.triggered_429 is True
        assert result.samples_to_429 == 5

    def test_baseline_429_signals_marker(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 4 + [_resp(429)]
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert "rate_limit:triggered_429" in result.signals

    def test_burst_429_stops_remaining_burst(self):
        # 12 baseline OK + 2 burst OK + 1 burst 429 → call_count == 15
        responses = [_resp(200, OPENAI_HEADERS)] * 14 + [_resp(429)] + [
            _resp(200, OPENAI_HEADERS)
        ]
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert c.raw_request.call_count == 15
        assert result.triggered_429 is True
        assert result.samples_to_429 == 15

    def test_envelope_429_detection_openai(self):
        body = '{"error":{"message":"rate","type":"rate_limit_error","code":"rate_limit"}}'
        responses = [_resp(429, {"retry-after": "30"}, body=body)]
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.envelope_429 == "openai-style"
        assert result.retry_after_pattern == "seconds"

    def test_envelope_429_detection_anthropic(self):
        body = '{"type":"error","error":{"type":"rate_limit_error","message":"slow down"}}'
        responses = [_resp(429, {}, body=body)]
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.envelope_429 == "anthropic-style"
        assert result.retry_after_pattern == "unknown"

    def test_envelope_429_absent_when_body_empty(self):
        responses = [_resp(429, {}, body="")]
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.envelope_429 == "absent"

    def test_envelope_429_absent_when_no_429(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.triggered_429 is False
        assert result.envelope_429 == "absent"


# ---------------------------------------------------------------------------
# rpm_observed extraction
# ---------------------------------------------------------------------------


class TestRpmObserved:
    def test_rpm_from_openai_header(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.rpm_observed == 60

    def test_rpm_from_anthropic_fallback(self):
        responses = [_resp(200, ANTHROPIC_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.rpm_observed == 50

    def test_rpm_none_when_no_known_header(self):
        responses = [_resp(200, {"server": "nginx"})] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.rpm_observed is None

    def test_rpm_none_when_header_not_integer(self):
        headers = {"x-ratelimit-limit-requests": "n/a"}
        responses = [_resp(200, headers)] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.rpm_observed is None


# ---------------------------------------------------------------------------
# compliance classification from real responses
# ---------------------------------------------------------------------------


class TestComplianceFromResponses:
    def test_openai_responses_classified_openai(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.compliance == "openai"
        assert "rate_limit:compliance_openai" in result.signals

    def test_anthropic_responses_classified_anthropic(self):
        responses = [_resp(200, ANTHROPIC_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.compliance == "anthropic"
        assert "rate_limit:compliance_anthropic" in result.signals

    def test_no_headers_classified_absent(self):
        responses = [_resp(200, {"server": "nginx"})] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.compliance == "absent"
        assert "rate_limit:compliance_absent" in result.signals

    def test_mixed_responses_openai_wins(self):
        responses = (
            [_resp(200, OPENAI_HEADERS)] * 8
            + [_resp(200, ANTHROPIC_HEADERS)] * 8
        )
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.compliance == "openai"


# ---------------------------------------------------------------------------
# headers_seen collation
# ---------------------------------------------------------------------------


class TestHeadersSeen:
    def test_headers_seen_lowercased_sorted_unique(self):
        h1 = {"X-RateLimit-Limit-Requests": "60"}
        h2 = {"x-ratelimit-remaining-requests": "59"}
        responses = [_resp(200, h1), _resp(200, h2)] + [_resp(200, h1)] * 14
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert "x-ratelimit-limit-requests" in result.headers_seen
        assert "x-ratelimit-remaining-requests" in result.headers_seen
        # Sorted + unique
        assert result.headers_seen == sorted(set(result.headers_seen))


# ---------------------------------------------------------------------------
# Transport error path
# ---------------------------------------------------------------------------


class TestTransportError:
    def test_all_transport_errors_yields_error_status(self):
        responses = [_resp(0, {}, error="connect: connection refused")] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.status == "error"
        assert result.error is not None
        assert result.error.code.startswith("transport_error")
        assert "rate_limit:transport_error" in result.signals

    def test_partial_transport_errors_still_ok(self):
        # 2 errors then 14 ok → status remains ok (probe continues past
        # transport errors; only complete failure escalates)
        responses = [_resp(0, {}, error="connect: connection refused")] * 2 + [
            _resp(200, OPENAI_HEADERS)
        ] * 14
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        assert result.status == "ok"
        assert result.compliance == "openai"


# ---------------------------------------------------------------------------
# Key safety (PRD §6.2 A8) — no raw key in result fields
# ---------------------------------------------------------------------------


class TestNoKeyLeak:
    def test_no_raw_key_appears_in_signals_or_error(self):
        responses = [_resp(0, {}, error=f"connect failed for Bearer {REAL_KEY}")] * 16
        c = _client(responses)
        clock = _FakeClock()
        result = rlfp.probe_rate_limit(
            c, enabled=True, sleep=clock.sleep, now=clock.now
        )
        # Build the full serialised footprint and assert key absence.
        from dataclasses import asdict
        payload = repr(asdict(result))
        assert REAL_KEY not in payload, (
            "Raw key leaked into RateLimitResult payload"
        )


# ---------------------------------------------------------------------------
# raw_request invocation contract
# ---------------------------------------------------------------------------


class TestRawRequestContract:
    def test_uses_client_raw_request_not_httpx_direct(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        rlfp.probe_rate_limit(c, enabled=True, sleep=clock.sleep, now=clock.now)
        # All 16 invocations must have gone through raw_request
        assert c.raw_request.call_count == 16

    def test_raw_request_called_with_bearer_token(self):
        responses = [_resp(200, OPENAI_HEADERS)] * 16
        c = _client(responses)
        clock = _FakeClock()
        rlfp.probe_rate_limit(c, enabled=True, sleep=clock.sleep, now=clock.now)
        # First call's headers must carry Bearer <api_key>
        first_call = c.raw_request.call_args_list[0]
        headers = first_call.kwargs.get("headers", {}) or (
            first_call.args[2] if len(first_call.args) > 2 else {}
        )
        joined = " ".join(f"{k}: {v}" for k, v in headers.items())
        assert "Bearer" in joined and REAL_KEY in joined
