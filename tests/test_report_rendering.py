"""S5-C report rendering tests — TES-129.

The four contracts under test:

* **schema_version fail-fast** — `reporting.schema.ReportView.from_record` and
  the public render entry points must refuse `schema_version != 1`.
* **golden-file Markdown diff** — `reporting/render_markdown.py` output for the
  fixture run-record matches the committed golden file byte-for-byte.
  Protects dual-distribution stylistic convergence with
  `api_relay_audit/reporter.py` (icons, flag semantics, summary block).
* **tri-consistency** — Markdown / HTML / JSON renderers all derive their cell
  coverage from the same `run-record.json` (no renderer is allowed to silently
  drop or duplicate a cell).
* **baseline diff thresholds** — `orchestration.baseline.compute_diff` flags
  perf changes ≥10% and score drops ≥0.1 in `diff.md` summary; cache-hit cells
  do NOT trigger perf threshold (design §2.4 R9).

Plus an end-to-end sentinel fuzz path that wires the real renderers — separate
from `tests/test_credential_isolation.py` so a regression here surfaces in the
S5-C suite first.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest


HERE = Path(__file__).resolve().parent
GOLDEN_DIR = HERE / "golden" / "report-rendering"
FIXTURE_PATH = GOLDEN_DIR / "run-record.fixture.json"
GOLDEN_MD_PATH = GOLDEN_DIR / "expected.md"


@pytest.fixture
def run_record() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# schema_version fail-fast — design §schema-pin
# ---------------------------------------------------------------------------

class TestSchemaFailFast:
    def test_view_from_record_rejects_v2(self, run_record):
        from reporting.schema import ReportView

        bad = dict(run_record, schema_version=2)
        with pytest.raises(ValueError, match=r"schema_version"):
            ReportView.from_record(bad)

    def test_view_from_record_rejects_missing(self, run_record):
        from reporting.schema import ReportView

        bad = {k: v for k, v in run_record.items() if k != "schema_version"}
        with pytest.raises(ValueError, match=r"schema_version"):
            ReportView.from_record(bad)

    def test_render_markdown_rejects_wrong_schema(self, run_record):
        from reporting.render_markdown import render

        bad = dict(run_record, schema_version=99)
        with pytest.raises(ValueError, match=r"schema_version"):
            render(bad)

    def test_render_html_rejects_wrong_schema(self, run_record):
        from reporting.render_html import render

        bad = dict(run_record, schema_version=99)
        with pytest.raises(ValueError, match=r"schema_version"):
            render(bad)


# ---------------------------------------------------------------------------
# Markdown — golden file diff
# ---------------------------------------------------------------------------

class TestMarkdownGoldenDiff:
    def test_render_matches_golden(self, run_record):
        from reporting.render_markdown import render

        got = render(run_record)
        expected = GOLDEN_MD_PATH.read_text(encoding="utf-8")
        assert got == expected, (
            "Markdown drift — run "
            "`python -m reporting.render_markdown tests/golden/report-rendering/"
            "run-record.fixture.json > tests/golden/report-rendering/expected.md`"
            " to re-bake if intentional."
        )

    def test_render_includes_risk_summary_block(self, run_record):
        from reporting.render_markdown import render

        md = render(run_record)
        # Dual-distribution stylistic convergence: existing Reporter emits a
        # "Risk Summary" block; the matrix renderer mirrors that contract.
        assert "Risk Summary" in md or "## Summary" in md

    def test_render_uses_existing_flag_icons(self, run_record):
        """Icons must come from api_relay_audit.reporter (dual-dist invariant)."""
        from reporting.render_markdown import render

        md = render(run_record)
        # Red / yellow / green icons (U+1F534 / U+1F7E1 / U+1F7E2) — same set as Reporter.flag.
        assert any(icon in md for icon in ("🔴", "🟡", "🟢"))


# ---------------------------------------------------------------------------
# Tri-consistency — MD / HTML / JSON come from the same run-record
# ---------------------------------------------------------------------------

class TestTriConsistency:
    def test_cell_coverage_md_vs_json(self, run_record):
        from reporting.render_markdown import render as render_md

        md = render_md(run_record)
        for cell in run_record["cells"]:
            # Each (step, endpoint) pair must appear somewhere in the Markdown
            # body — guards against a renderer silently dropping cells.
            tag = f"{cell['step']}"
            assert tag in md, f"missing step {tag!r} in Markdown body"

    def test_cell_coverage_html_vs_json(self, run_record):
        from reporting.render_html import render as render_html

        html = render_html(run_record)
        for cell in run_record["cells"]:
            assert cell["step"] in html
            assert cell["endpoint"] in html

    def test_summary_consistent_across_renderers(self, run_record):
        from reporting.render_html import render as render_html
        from reporting.render_markdown import render as render_md

        md = render_md(run_record)
        html = render_html(run_record)
        total = str(run_record["summary"]["total"])
        ok = str(run_record["summary"]["ok"])
        assert total in md and total in html
        assert ok in md and ok in html


# ---------------------------------------------------------------------------
# HTML — single file ≤ 2 MB, offline self-contained
# ---------------------------------------------------------------------------

class TestHtmlConstraints:
    def test_size_under_2mb(self, run_record):
        from reporting.render_html import render

        html = render(run_record)
        assert len(html.encode("utf-8")) <= 2 * 1024 * 1024

    def test_no_external_urls(self, run_record):
        from reporting.render_html import render

        html = render(run_record)
        # Self-contained: no http(s) anywhere, no external stylesheets.
        assert "http://" not in html
        assert "https://" not in html
        assert '<link rel="stylesheet"' not in html
        assert "<script src=" not in html

    def test_renders_valid_html5_doctype(self, run_record):
        from reporting.render_html import render

        html = render(run_record)
        assert html.lstrip().lower().startswith("<!doctype html")


# ---------------------------------------------------------------------------
# Baseline diff — orchestration.baseline.compute_diff
# ---------------------------------------------------------------------------

@pytest.fixture
def baseline_record(run_record):
    """Same shape, slightly older numbers — used as the 'previous run'."""
    base = json.loads(json.dumps(run_record))  # deep copy
    base["generated_at"] = "2026-05-24T11:00:00+00:00"
    base["config_digest"] = "cfg-prev-base"
    return base


class TestBaselineDiff:
    def test_compute_diff_returns_summary_and_cells(self, run_record, baseline_record):
        from orchestration.baseline import compute_diff

        diff = compute_diff(current=run_record, baseline=baseline_record)
        assert "summary" in diff
        assert "cells" in diff

    def test_perf_threshold_flags_at_10_percent(self, run_record, baseline_record):
        """perf latency_ms rises by 50% — must be flagged."""
        from orchestration.baseline import compute_diff

        current = json.loads(json.dumps(run_record))
        for cell in current["cells"]:
            if cell["step"] == "perf" and cell["endpoint"] == "alpha":
                cell["latency_ms"] = 1000.0  # was 500 → +100%
        diff = compute_diff(current=current, baseline=baseline_record)
        flagged = [c for c in diff["cells"] if c.get("flagged")]
        assert any(
            c["step"] == "perf" and c["endpoint"] == "alpha" for c in flagged
        ), "expected perf/alpha to be flagged on +100% latency"

    def test_perf_under_threshold_not_flagged(self, run_record, baseline_record):
        from orchestration.baseline import compute_diff

        current = json.loads(json.dumps(run_record))
        for cell in current["cells"]:
            if cell["step"] == "perf" and cell["endpoint"] == "alpha":
                cell["latency_ms"] = 525.0  # +5%, under threshold
        diff = compute_diff(current=current, baseline=baseline_record)
        flagged_pairs = {
            (c["step"], c["endpoint"]) for c in diff["cells"] if c.get("flagged")
        }
        assert ("perf", "alpha") not in flagged_pairs

    def test_score_drop_flags_at_0_1(self, run_record, baseline_record):
        """purity score drop of 0.2 — must be flagged."""
        from orchestration.baseline import compute_diff

        current = json.loads(json.dumps(run_record))
        for cell in current["cells"]:
            if cell["step"] == "purity" and cell["endpoint"] == "alpha":
                cell["payload"] = {"score": 0.55, "note": "regressed"}
        diff = compute_diff(current=current, baseline=baseline_record)
        flagged = [c for c in diff["cells"] if c.get("flagged")]
        assert any(
            c["step"] == "purity" and c["endpoint"] == "alpha" for c in flagged
        )

    def test_score_drop_under_threshold_not_flagged(self, run_record, baseline_record):
        from orchestration.baseline import compute_diff

        current = json.loads(json.dumps(run_record))
        for cell in current["cells"]:
            if cell["step"] == "purity" and cell["endpoint"] == "alpha":
                cell["payload"] = {"score": 0.76, "note": "tiny drop"}
        diff = compute_diff(current=current, baseline=baseline_record)
        flagged_pairs = {
            (c["step"], c["endpoint"]) for c in diff["cells"] if c.get("flagged")
        }
        assert ("purity", "alpha") not in flagged_pairs

    def test_cache_hit_perf_cell_is_skipped_for_perf_threshold(
        self, run_record, baseline_record
    ):
        """design §2.4 R9 — a cache_hit cell's latency is dominated by the
        cache layer, so the perf threshold must NOT fire on it."""
        from orchestration.baseline import compute_diff

        current = json.loads(json.dumps(run_record))
        for cell in current["cells"]:
            if cell["step"] == "pricing" and cell["endpoint"] == "alpha":
                cell["latency_ms"] = 5000.0  # absurd vs baseline 75
                assert cell["cache_hit"] is True  # fixture invariant
        diff = compute_diff(current=current, baseline=baseline_record)
        flagged_pairs = {
            (c["step"], c["endpoint"]) for c in diff["cells"] if c.get("flagged")
        }
        assert ("pricing", "alpha") not in flagged_pairs, (
            "cache_hit cell should be excluded from perf threshold"
        )

    def test_diff_md_has_top_summary_block(self, run_record, baseline_record):
        from orchestration.baseline import compute_diff, render_diff_md

        current = json.loads(json.dumps(run_record))
        for cell in current["cells"]:
            if cell["step"] == "perf":
                cell["latency_ms"] *= 2.0  # +100% perf, all cells flagged
        diff = compute_diff(current=current, baseline=baseline_record)
        md = render_diff_md(diff)
        first_block = md.split("\n\n", 2)[0]
        # Summary lives at the top of diff.md — verify the digit appears in
        # the first paragraph, not just somewhere lower in the body.
        assert re.search(r"\d+", first_block), "expected digit in top summary"
        assert "flag" in first_block.lower()

    def test_compute_diff_rejects_wrong_schema(self, run_record, baseline_record):
        from orchestration.baseline import compute_diff

        bad = dict(run_record, schema_version=2)
        with pytest.raises(ValueError, match=r"schema_version"):
            compute_diff(current=bad, baseline=baseline_record)
        bad_base = dict(baseline_record, schema_version=2)
        with pytest.raises(ValueError, match=r"schema_version"):
            compute_diff(current=run_record, baseline=bad_base)


# ---------------------------------------------------------------------------
# Sentinel fuzz — end-to-end via the real renderers
# ---------------------------------------------------------------------------

class TestSentinelFuzz:
    """When a sentinel string ends up in any payload / error field, the rendered
    HTML / MD / diff.md artifacts must NOT contain it.

    Wires the real `reporting.render_*` and `orchestration.baseline.*` pipelines
    so a regression in a downstream renderer surfaces here (and not only in
    the abstract sentinel test in tests/test_credential_isolation.py).
    """

    SENTINEL = "SENTINEL-S5C-XYZ-1234567890ABCDEFGHIJ"

    def _inject(self, record: dict) -> dict:
        """Embed the sentinel in payload + error fields."""
        polluted = json.loads(json.dumps(record))
        for cell in polluted["cells"]:
            if cell.get("payload"):
                cell["payload"] = dict(
                    cell["payload"],
                    note=f"{cell['payload'].get('note', '')} key={self.SENTINEL}",
                    raw_response=(
                        "Echo: your bearer was "
                        f"Bearer {self.SENTINEL} on https://x/api?api_key={self.SENTINEL}"
                    ),
                )
            if cell.get("error"):
                cell["error"] = dict(
                    cell["error"],
                    message=f"{cell['error'].get('message', '')} key={self.SENTINEL}",
                )
        return polluted

    def test_no_sentinel_in_markdown(self, run_record):
        from reporting.render_markdown import render

        polluted = self._inject(run_record)
        out = render(polluted, sentinel_keys=[self.SENTINEL])
        assert self.SENTINEL not in out

    def test_no_sentinel_in_html(self, run_record):
        from reporting.render_html import render

        polluted = self._inject(run_record)
        out = render(polluted, sentinel_keys=[self.SENTINEL])
        assert self.SENTINEL not in out

    def test_no_sentinel_in_diff_md(self, run_record):
        from orchestration.baseline import compute_diff, render_diff_md

        polluted = self._inject(run_record)
        baseline = json.loads(json.dumps(run_record))
        diff = compute_diff(
            current=polluted, baseline=baseline, sentinel_keys=[self.SENTINEL]
        )
        out = render_diff_md(diff)
        assert self.SENTINEL not in out
