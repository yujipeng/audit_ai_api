"""Red-phase tests for api_relay_audit.evaluator.reporter.

S2-4 切片 A：Reporter JSON v2.0 + HTML 六段渲染契约。
- AC-F2: JSON schema_version + dimensions keys + overall.verdict 字段命中
- AC-F5: raw_keep=False → 输出无 raw_artifacts 字段
- AC-F6: HTML 报告六维各独立段落
- AC-N2: HTML 单文件 ≤ 500 KB
- AC-N3: JSON --raw-keep=false ≤ 50 KB
"""
from __future__ import annotations

import json

import pytest

from api_relay_audit.evaluator.aggregator import PurityAggregator
from api_relay_audit.evaluator.base import EvaluatorResult, ProbeContext


# Red-phase: this import must fail until 切片 B 落地 reporter.py
from api_relay_audit.evaluator.reporter import (  # noqa: E402
    render_html,
    render_json,
    report_to_dict,
)


def _ctx(vendor="claude"):
    return ProbeContext(
        client=None,
        api_key="sk-fake-1234",
        base_url="https://example.com",
        vendor=vendor,
        rounds=2,
        prompts=[],
        sleep_between=0.0,
        keep_raw=False,
    )


def _clean_result(dim, rounds_ok=10, rounds_total=10, raw=None):
    return EvaluatorResult(
        dimension=dim,
        verdict="clean",
        verdict_reason=f"{dim}: clean",
        rounds_total=rounds_total,
        rounds_ok=rounds_ok,
        hits=[],
        duration_s=0.5,
        raw_artifacts=raw,
    )


def _aggregate(results):
    return PurityAggregator().aggregate(_ctx(), results)


class TestRenderJsonEmpty:
    def test_schema_version_locked(self):
        report = _aggregate([])
        out = render_json(report, raw_keep=False)
        data = json.loads(out)
        assert data["schema_version"] == "purity-v2.0"

    def test_empty_dimensions_dict(self):
        report = _aggregate([])
        data = json.loads(render_json(report, raw_keep=False))
        assert data["dimensions"] == {}

    def test_empty_overall_verdict_clean(self):
        report = _aggregate([])
        data = json.loads(render_json(report, raw_keep=False))
        assert data["overall"]["verdict"] == "clean"


class TestRenderJsonRawKeep:
    def test_raw_keep_false_strips_raw_artifacts(self):
        results = [
            _clean_result("injection", raw=[{"prompt": "x", "response": "y"}]),
            _clean_result("swap", raw=[{"snapshot": "z"}]),
        ]
        report = _aggregate(results)
        out = render_json(report, raw_keep=False)
        data = json.loads(out)
        for dim_data in data["dimensions"].values():
            assert "raw_artifacts" not in dim_data or dim_data["raw_artifacts"] is None

    def test_raw_keep_true_preserves_raw_artifacts(self):
        results = [_clean_result("injection", raw=[{"prompt": "p1"}])]
        report = _aggregate(results)
        out = render_json(report, raw_keep=True)
        data = json.loads(out)
        assert data["dimensions"]["injection"]["raw_artifacts"] == [{"prompt": "p1"}]

    def test_raw_keep_false_size_ceiling_50kb(self):
        # AC-N3: with raw stripped, JSON for 6-dim full clean report ≤ 50 KB
        dims = ["injection", "swap", "drift", "tool", "refusal", "leak"]
        results = [_clean_result(d, raw=None) for d in dims]
        report = _aggregate(results)
        out = render_json(report, raw_keep=False)
        assert len(out.encode("utf-8")) <= 50 * 1024


class TestRenderHtmlSections:
    def test_six_dimension_sections_present(self):
        # AC-F6: 六维各独立段落（web3 default-off 不强制）
        dims = ["injection", "swap", "drift", "tool", "refusal", "leak"]
        results = [_clean_result(d) for d in dims]
        report = _aggregate(results)
        html = render_html(report)
        assert isinstance(html, str)
        for d in dims:
            assert f'data-dim="{d}"' in html, f"missing section for {d}"

    def test_html_size_ceiling_500kb(self):
        # AC-N2: HTML 单文件 ≤ 500 KB
        dims = ["injection", "swap", "drift", "tool", "refusal", "leak"]
        results = [_clean_result(d) for d in dims]
        report = _aggregate(results)
        html = render_html(report)
        assert len(html.encode("utf-8")) <= 500 * 1024

    def test_html_escapes_snippet_html(self):
        result = EvaluatorResult(
            dimension="injection",
            verdict="suspicious",
            verdict_reason="snippet with <script>",
            rounds_total=2,
            rounds_ok=0,
            hits=[{"snippet": "<script>alert(1)</script>", "round": 1}],
            duration_s=0.1,
            raw_artifacts=None,
        )
        report = _aggregate([result])
        html = render_html(report)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestReportToDictDeterminism:
    def test_two_runs_diff_only_on_metadata(self):
        # AC-R3: same input twice → diff = 0 lines (excluding metadata timestamps)
        results = [_clean_result(d) for d in ("injection", "swap", "drift")]
        d1 = report_to_dict(_aggregate(results), raw_keep=False)
        d2 = report_to_dict(_aggregate(results), raw_keep=False)
        for d in (d1, d2):
            d["metadata"].pop("started_at", None)
            d["metadata"].pop("duration_s", None)
            d["metadata"].pop("evaluators_run", None)
        assert d1 == d2
