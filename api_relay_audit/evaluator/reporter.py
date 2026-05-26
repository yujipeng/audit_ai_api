"""Reporter — purity-v2.0 JSON + HTML 六段渲染（design §4.2）.

公开 API:
- ``report_to_dict(report, *, raw_keep)`` — PurityReportV2 → dict
- ``render_json(report, *, raw_keep, indent=2)`` — dict → JSON 字符串（sort_keys，AC-R3 稳定序）
- ``render_html(report)`` — dict → 单文件 HTML（六段锚点 + 内联 CSS）

约束:
- AC-F2 schema_version 字面值 = ``"purity-v2.0"``
- AC-F5 ``raw_keep=False`` → 各维度 ``raw_artifacts`` 字段被剔除
- AC-F6 默认六维各独立 ``<section data-dim="...">`` 段落
- AC-N2 HTML ≤ 500 KB；AC-N3 JSON（raw_keep=False）≤ 50 KB
- AC-R3 同输入两次输出除 metadata 时间戳外字节级一致（sort_keys + 维度名固定序）
"""
from __future__ import annotations

import dataclasses
import html
import json
from string import Template
from typing import Any

from .aggregator import Overall, PurityReportV2
from .base import EvaluatorResult


_DEFAULT_DIMENSIONS: tuple[str, ...] = (
    "injection",
    "swap",
    "drift",
    "tool",
    "refusal",
    "leak",
)
_SNIPPET_MAX = 200


def report_to_dict(report: PurityReportV2, *, raw_keep: bool) -> dict[str, Any]:
    """Serialize PurityReportV2 → dict, applying raw_keep stripping."""
    dims_out: dict[str, Any] = {}
    for dim_id, result in sorted(report.dimensions.items()):
        dim_dict = _result_to_dict(result, raw_keep=raw_keep)
        dims_out[dim_id] = dim_dict

    overall = report.overall
    overall_dict = {
        "verdict": overall.verdict,
        "verdict_reason": overall.verdict_reason,
        "confidence": overall.confidence,
        "worst_dimensions": list(overall.worst_dimensions),
    }

    return {
        "schema_version": report.schema_version,
        "metadata": dict(report.metadata),
        "dimensions": dims_out,
        "overall": overall_dict,
    }


def _result_to_dict(result: EvaluatorResult, *, raw_keep: bool) -> dict[str, Any]:
    base = {
        "dimension": result.dimension,
        "verdict": result.verdict,
        "verdict_reason": result.verdict_reason,
        "rounds_total": result.rounds_total,
        "rounds_ok": result.rounds_ok,
        "hits": [dict(h) for h in result.hits],
        "duration_s": result.duration_s,
    }
    if raw_keep:
        base["raw_artifacts"] = (
            [dict(item) for item in result.raw_artifacts]
            if result.raw_artifacts is not None
            else None
        )
    return base


def render_json(
    report: PurityReportV2,
    *,
    raw_keep: bool = False,
    indent: int = 2,
) -> str:
    data = report_to_dict(report, raw_keep=raw_keep)
    return json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        indent=indent,
        default=_json_default,
    )


def _json_default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj):
        return dataclasses.asdict(obj)
    if isinstance(obj, (set, tuple)):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


_HTML_TEMPLATE = Template(
    """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"/>
<title>Purity Report — $target</title>
<style>
body{font-family:system-ui,-apple-system,sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{margin:0 0 8px}
h2{margin:24px 0 8px;font-size:18px;border-bottom:1px solid #d0d7de;padding-bottom:4px}
.meta{color:#57606a;font-size:13px;margin-bottom:16px}
.verdict{display:inline-block;padding:2px 8px;border-radius:4px;font-weight:600;font-size:12px;text-transform:uppercase}
.v-clean{background:#dafbe1;color:#1a7f37}
.v-suspicious{background:#fff8c5;color:#9a6700}
.v-injected{background:#ffebe9;color:#cf222e}
.v-failed{background:#ddf4ff;color:#0969da}
.v-unknown{background:#eaeef2;color:#57606a}
section{padding:8px 0}
.hits{margin-top:8px;font-size:13px}
.snippet{font-family:ui-monospace,monospace;background:#f6f8fa;padding:6px;border-radius:4px;display:block;white-space:pre-wrap;word-break:break-all}
</style></head><body>
<h1>Purity Report</h1>
<div class="meta">target=$target | vendor=$vendor | rounds=$rounds | started=$started</div>
<section data-dim="overall">
<h2>Overall <span class="verdict v-$overall_verdict">$overall_verdict</span></h2>
<div>reason: $overall_reason</div>
<div>confidence: $overall_confidence</div>
<div>worst_dimensions: $overall_worst</div>
</section>
$dimension_sections
</body></html>"""
)

_DIM_SECTION_TPL = Template(
    """<section data-dim="$dim">
<h2>$dim <span class="verdict v-$verdict">$verdict</span></h2>
<div>reason: $reason</div>
<div>rounds_ok: $rounds_ok / $rounds_total | duration_s: $duration_s</div>
$hits_block
</section>"""
)


def render_html(report: PurityReportV2) -> str:
    metadata = report.metadata
    overall = report.overall

    sections: list[str] = []
    for dim_id in _ordered_dim_ids(report):
        result = report.dimensions[dim_id]
        sections.append(_render_dim_section(dim_id, result))

    return _HTML_TEMPLATE.substitute(
        target=html.escape(str(metadata.get("target_url", ""))),
        vendor=html.escape(str(metadata.get("vendor", ""))),
        rounds=html.escape(str(metadata.get("rounds_requested", ""))),
        started=html.escape(str(metadata.get("started_at", ""))),
        overall_verdict=html.escape(overall.verdict),
        overall_reason=html.escape(overall.verdict_reason),
        overall_confidence=f"{overall.confidence:.4f}",
        overall_worst=html.escape(", ".join(overall.worst_dimensions) or "-"),
        dimension_sections="\n".join(sections),
    )


def _ordered_dim_ids(report: PurityReportV2) -> list[str]:
    present = set(report.dimensions.keys())
    ordered = [d for d in _DEFAULT_DIMENSIONS if d in present]
    extras = sorted(d for d in present if d not in _DEFAULT_DIMENSIONS)
    return ordered + extras


def _render_dim_section(dim_id: str, result: EvaluatorResult) -> str:
    hits_block = _render_hits_block(result.hits)
    return _DIM_SECTION_TPL.substitute(
        dim=html.escape(dim_id),
        verdict=html.escape(result.verdict),
        reason=html.escape(result.verdict_reason),
        rounds_ok=result.rounds_ok,
        rounds_total=result.rounds_total,
        duration_s=f"{result.duration_s:.3f}",
        hits_block=hits_block,
    )


def _render_hits_block(hits: list[dict]) -> str:
    if not hits:
        return ""
    rows: list[str] = ['<div class="hits">']
    for hit in hits:
        snippet_raw = str(hit.get("snippet", ""))
        if len(snippet_raw) > _SNIPPET_MAX:
            snippet_raw = snippet_raw[:_SNIPPET_MAX] + "…"
        snippet = html.escape(snippet_raw)
        round_no = html.escape(str(hit.get("round", "")))
        rows.append(
            f'<div>round {round_no}: <code class="snippet">{snippet}</code></div>'
        )
    rows.append("</div>")
    return "\n".join(rows)


__all__ = ["render_html", "render_json", "report_to_dict"]
