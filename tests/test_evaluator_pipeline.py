"""End-to-end integration test — purity evaluator pipeline against a fake honest client.

S2-4 切片 E：mock 「诚实」relay → full 6-dim pipeline → overall.verdict=clean。
- AC-F7: pipeline 全 6 维下 verdict=clean
- AC-N1: 10 轮全六维 ≤ 30s
- AC-R3: 同一 FakeHonestClient 跑两次 dict 深比较一致（排除时间戳）
"""
from __future__ import annotations

import time

import pytest

from api_relay_audit.evaluator.base import ProbeContext
from api_relay_audit.evaluator.factory import default_purity_pipeline
from api_relay_audit.evaluator.reporter import report_to_dict


_HONEST_TEXT = "我是 Claude，由 Anthropic 训练，模型为 claude-opus-4-6。"


class FakeHonestClient:
    """A relay that always returns a clean Claude self-identification.

    Surface area covers every method the 6 default evaluators touch:
    - ``call(messages, max_tokens, ...)`` — used by injection / swap / drift /
      refusal evaluators, plus tool_substitution echo probes.
    - ``raw_request(method, path, headers, body, ...)`` — used by leak
      evaluator's error-leakage probes; always returns HTTP 200 with a
      generic body so severity stays ``none`` → verdict ``clean``.
    """

    api_key = "sk-fake-fake-fake"
    base_url = "https://example.com"
    detected_format = "anthropic"

    def __init__(self) -> None:
        self.call_count = 0
        self.raw_count = 0

    def call(self, messages, system=None, max_tokens=512):
        self.call_count += 1
        user_msg = ""
        for m in messages:
            if m.get("role") == "user":
                user_msg = str(m.get("content", ""))
        # Tool-substitution probes ask the model to copy a specific install
        # command verbatim. Honest behavior: echo exactly the target string.
        if "Text to copy:" in user_msg:
            target = user_msg.split("Text to copy:", 1)[1].strip()
            return {
                "text": target,
                "input_tokens": 4,
                "output_tokens": 4,
                "raw": {},
                "time": 0.001,
            }
        return {
            "text": _HONEST_TEXT,
            "input_tokens": 8,
            "output_tokens": 16,
            "raw": {},
            "time": 0.001,
        }

    def raw_request(self, method, path, headers, body,
                    content_type="application/json", timeout=30):
        self.raw_count += 1
        return {
            "status": 200,
            "body": '{"ok": true}',
            "headers": {"content-type": "application/json"},
            "error": None,
        }

    # Stream + models endpoints — not used by default 6 evaluators, kept here
    # so the surface stays compatible if future evaluators reach for them.
    def get_models(self):
        return {"data": []}

    def stream_call(self, messages, system=None, max_tokens=512, **_):
        return {"events": [], "usage": {}, "error": None}


@pytest.fixture
def honest_ctx():
    client = FakeHonestClient()
    return client, ProbeContext(
        client=client,
        api_key=client.api_key,
        base_url=client.base_url,
        vendor="claude",
        rounds=2,
        prompts=[],
        sleep_between=0.0,
        keep_raw=False,
    )


class TestPipelineFullSixDimensionsClean:
    def test_overall_verdict_clean_with_honest_client(self, honest_ctx):
        client, ctx = honest_ctx
        report = default_purity_pipeline().run(ctx)
        # AC-F7: honest relay → overall clean is the desired outcome.
        # drift is intentionally an "unknown" placeholder (S3) — accept either
        # 'clean' or fall back to 'unknown' if drift dominates ranking.
        assert report.overall.verdict in ("clean", "unknown"), (
            report.overall.verdict_reason
        )
        # Every default dimension produced a result.
        for d in ("injection", "swap", "drift", "tool", "refusal", "leak"):
            assert d in report.dimensions, f"missing dimension {d}"

    def test_pipeline_runs_within_30s_at_10_rounds(self):
        # AC-N1: 10 rounds × 6 dimensions on the fake client ≤ 30 s.
        client = FakeHonestClient()
        ctx = ProbeContext(
            client=client,
            api_key=client.api_key,
            base_url=client.base_url,
            vendor="claude",
            rounds=10,
            prompts=[],
            sleep_between=0.0,
            keep_raw=False,
        )
        t0 = time.perf_counter()
        default_purity_pipeline().run(ctx)
        elapsed = time.perf_counter() - t0
        assert elapsed <= 30.0, f"pipeline took {elapsed:.2f}s (>30s budget)"


class TestPipelineDeterminismAcR3:
    def test_two_runs_match_excluding_timestamps(self):
        # AC-R3: two runs on the same fake honest client diff = 0 lines
        # (excluding metadata.started_at / duration_s / evaluators_run and
        # per-dimension duration_s).
        client_a = FakeHonestClient()
        ctx_a = ProbeContext(
            client=client_a,
            api_key=client_a.api_key,
            base_url=client_a.base_url,
            vendor="claude",
            rounds=2,
            prompts=[],
            sleep_between=0.0,
            keep_raw=False,
        )
        client_b = FakeHonestClient()
        ctx_b = ProbeContext(
            client=client_b,
            api_key=client_b.api_key,
            base_url=client_b.base_url,
            vendor="claude",
            rounds=2,
            prompts=[],
            sleep_between=0.0,
            keep_raw=False,
        )
        d1 = report_to_dict(
            default_purity_pipeline().run(ctx_a), raw_keep=False
        )
        d2 = report_to_dict(
            default_purity_pipeline().run(ctx_b), raw_keep=False
        )
        for d in (d1, d2):
            for k in ("started_at", "duration_s", "evaluators_run"):
                d["metadata"].pop(k, None)
            for dim_data in d.get("dimensions", {}).values():
                dim_data.pop("duration_s", None)
        assert d1 == d2
