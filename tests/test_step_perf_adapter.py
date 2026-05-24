"""AC-S5-012 — real perf adapter wired to api_relay_audit.perf.runner.

The adapter delegates to `api_relay_audit.perf.runner._run_rounds_for_model`,
which is the existing perf-bench machinery. The test stubs the inner streaming
call so we don't hit the network but still exercise the full wiring (param
translation, payload shape, redaction).
"""

from __future__ import annotations

from unittest import mock

from orchestration.steps import EndpointSpec, StepContext, step_perf_real
from security.bearer import CredentialBearer


class _FakeStreamResult:
    """Lookalike for api_relay_audit.perf.streaming.StreamResult."""

    def __init__(self, *, ok: bool = True, text: str = "hello world",
                 ttft: float = 0.1, total: float = 0.5):
        self.ok = ok
        self.status_code = 200 if ok else 500
        self.ttft = ttft
        self.total_time = total
        self.text = text
        self.chunk_count = 5
        self.finish_reason = "stop"
        self.error = None
        self.format = "openai"


def test_step_perf_real_delegates_to_runner_and_shapes_payload():
    """Stub `_execute_one` -> exercise param translation + payload assembly."""
    ctx = StepContext(
        step="perf",
        endpoint=EndpointSpec(
            name="ep-test",
            base_url="https://relay.example.com/v1",
            credential=CredentialBearer("sk-fake-NOT-A-REAL-KEY-1234567890"),
        ),
        model="claude-haiku-4-5",
        run_params={"rounds": 2, "max_tokens": 32, "timeout": 5.0,
                    "prompt": "ping", "mock": False},
    )

    with mock.patch(
        "api_relay_audit.perf.runner._execute_one",
        return_value=_FakeStreamResult(),
    ):
        result = step_perf_real(ctx)

    assert result["status"] == "ok"
    assert result["step"] == "perf"
    assert result["model"] == "claude-haiku-4-5"
    assert result["endpoint"] == "ep-test"

    payload = result["payload"]
    # payload comes from api_relay_audit.perf.runner._run_rounds_for_model
    assert "rounds" in payload
    assert "metrics" in payload
    assert "purity" in payload
    assert len(payload["rounds"]) == 2  # 2 rounds requested
    # Per-round metrics propagated.
    assert payload["metrics"]["success_rate"] == 1.0
    assert payload["metrics"]["successful_rounds"] == 2


def test_step_perf_real_handles_failed_rounds_via_metrics():
    ctx = StepContext(
        step="perf",
        endpoint=EndpointSpec(
            name="ep-fail",
            base_url="https://broken.example.com/v1",
            credential=CredentialBearer("sk-fake-NOT-A-REAL-KEY-1234567890"),
        ),
        model="some-model",
        run_params={"rounds": 1, "max_tokens": 16, "timeout": 2.0,
                    "prompt": "ping", "mock": False},
    )

    failed = _FakeStreamResult(ok=False, ttft=None)
    failed.error = "simulated upstream error"
    with mock.patch(
        "api_relay_audit.perf.runner._execute_one",
        return_value=failed,
    ):
        result = step_perf_real(ctx)

    assert result["status"] == "ok"  # adapter ran cleanly; the cell *itself* did not error
    payload = result["payload"]
    assert payload["metrics"]["success_rate"] == 0.0
    assert payload["metrics"]["failed_rounds"] == 1
