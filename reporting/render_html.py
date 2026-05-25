"""Single-file, self-contained HTML rendering for audit-matrix run-record.json.

Hard constraints (AC-S5-007):
* Single file ≤ 2 MB total (UTF-8 bytes).
* Offline / disconnected — no `<link>`, `<script src=...>`, no `http(s)://`.
* Inline CSS + vanilla JS only; renders fully without a network round-trip.

Structure (inspired by `web/data-example.json` view: rating colour, summary
panel, per-cell cards):
* Header strip with metadata + summary chips.
* Four step sections (probe, purity, perf, pricing) each with a sortable
  / filter-able table of (endpoint, model, status, latency, cache_hit, score).
* A `Diff` placeholder div — the baseline diff renderer (orchestration.baseline)
  hydrates this when paired with diff.md, but offline HTML still parses.

We deliberately avoid any third-party JS framework; the size budget would not
survive a React bundle, and the audit consumer is a security tool — minimal
attack surface beats ergonomic templates.
"""

from __future__ import annotations

import html as _html
import json
from typing import Any, Iterable, List, Mapping, Sequence

from reporting.schema import (
    CellView,
    ReportView,
    SUPPORTED_SCHEMA_VERSION,
    all_sentinels,
)
from security.redact import redact_response_text, redact_text

PAYLOAD_NOTE_MAX = 240
RATING_ICON = {"red": "🔴", "yellow": "🟡", "green": "🟢"}

_INLINE_CSS = """
:root { color-scheme: light dark; }
body { font: 14px/1.5 system-ui, sans-serif; margin: 24px; max-width: 1200px; }
h1 { font-size: 22px; margin: 0 0 8px; }
h2 { font-size: 18px; margin: 24px 0 8px; border-bottom: 1px solid #ccc; padding-bottom: 4px; }
.meta { color: #555; font-size: 13px; }
.meta code { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }
.chip { display: inline-block; padding: 2px 8px; margin: 2px 4px 2px 0; border-radius: 12px; background: #eef; font-size: 12px; }
.chip.red { background: #fde2e2; color: #8a1a1a; }
.chip.yellow { background: #fff3cd; color: #6a4f00; }
.chip.green { background: #e2f5e9; color: #1a6a36; }
table { width: 100%; border-collapse: collapse; margin-top: 6px; font-size: 13px; }
th, td { padding: 6px 8px; border-bottom: 1px solid #e0e0e0; text-align: left; vertical-align: top; }
th { background: #f7f7f7; cursor: pointer; user-select: none; }
th[data-sorted="asc"]::after { content: " ▲"; }
th[data-sorted="desc"]::after { content: " ▼"; }
tr.row-red { background: #fff5f5; }
tr.row-yellow { background: #fffaf0; }
tr.row-green { background: #f5fbf6; }
.note { font-family: ui-monospace, monospace; font-size: 12px; color: #444; word-break: break-all; }
.filter { margin: 8px 0; }
.filter input { padding: 4px 8px; font-size: 13px; width: 200px; }
.diff { padding: 12px; background: #fafafa; border: 1px dashed #ccc; border-radius: 4px; }
@media (prefers-color-scheme: dark) {
  body { background: #181818; color: #eaeaea; }
  th { background: #232323; }
  td, th { border-color: #333; }
  .meta code { background: #2a2a2a; }
  .chip { background: #2a2a3a; color: #eee; }
  tr.row-red { background: #3a1414; }
  tr.row-yellow { background: #3a2f10; }
  tr.row-green { background: #143a1c; }
  .diff { background: #1f1f1f; border-color: #444; }
}
""".strip()

_INLINE_JS = """
(function () {
  function bind(table) {
    var ths = table.querySelectorAll('th');
    ths.forEach(function (th, i) {
      th.addEventListener('click', function () {
        var dir = th.getAttribute('data-sorted') === 'asc' ? 'desc' : 'asc';
        ths.forEach(function (o) { o.removeAttribute('data-sorted'); });
        th.setAttribute('data-sorted', dir);
        var rows = Array.from(table.tBodies[0].rows);
        rows.sort(function (a, b) {
          var av = a.cells[i].innerText.trim();
          var bv = b.cells[i].innerText.trim();
          var an = parseFloat(av); var bn = parseFloat(bv);
          if (!isNaN(an) && !isNaN(bn)) { return dir === 'asc' ? an - bn : bn - an; }
          return dir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av);
        });
        rows.forEach(function (r) { table.tBodies[0].appendChild(r); });
      });
    });
    var filter = table.previousElementSibling;
    if (filter && filter.classList.contains('filter')) {
      var input = filter.querySelector('input');
      input.addEventListener('input', function () {
        var q = input.value.toLowerCase();
        Array.from(table.tBodies[0].rows).forEach(function (r) {
          r.style.display = r.innerText.toLowerCase().indexOf(q) >= 0 ? '' : 'none';
        });
      });
    }
  }
  document.querySelectorAll('table.matrix').forEach(bind);
})();
""".strip()


def _esc(s: Any) -> str:
    return _html.escape(str(s), quote=True)


def _redact(s: str, sentinels: Sequence[str]) -> str:
    if not s:
        return s
    return redact_text(redact_response_text(s, set(sentinels)))


def _payload_summary(payload: Mapping[str, Any], sentinels: Sequence[str]) -> str:
    if not payload:
        return ""
    parts: List[str] = []
    score = payload.get("score")
    if score is not None:
        parts.append(f"score={score}")
    note = payload.get("note")
    if note:
        parts.append(_redact(str(note)[:PAYLOAD_NOTE_MAX], sentinels))
    raw = payload.get("raw_response")
    if raw:
        parts.append(_redact(str(raw)[:PAYLOAD_NOTE_MAX], sentinels))
    return " · ".join(parts)


def _row(cell: CellView, sentinels: Sequence[str]) -> str:
    score = cell.payload.get("score") if cell.payload else None
    note_html = _esc(_payload_summary(cell.payload, sentinels))
    return (
        f'<tr class="row-{_esc(cell.rating)}">'
        f'<td>{RATING_ICON.get(cell.rating, "⚪")} {_esc(cell.endpoint)}</td>'
        f'<td>{_esc(cell.model)}</td>'
        f'<td>{_esc(cell.status)}</td>'
        f'<td>{cell.latency_ms:.1f}</td>'
        f'<td>{"yes" if cell.cache_hit else "no"}</td>'
        f'<td>{_esc("" if score is None else score)}</td>'
        f'<td><code>{_esc(cell.redacted_key_id)}</code></td>'
        f'<td><span class="note">{note_html}</span></td>'
        f"</tr>"
    )


def _step_section(step: str, cells: List[CellView], sentinels: Sequence[str]) -> str:
    rows = "\n".join(_row(c, sentinels) for c in cells)
    return f"""
<h2>{_esc(step)}</h2>
<div class="filter"><input type="search" placeholder="filter rows…" aria-label="filter {_esc(step)} rows"></div>
<table class="matrix">
  <thead><tr>
    <th>endpoint</th><th>model</th><th>status</th><th>latency (ms)</th>
    <th>cache</th><th>score</th><th>key</th><th>note</th>
  </tr></thead>
  <tbody>
{rows}
  </tbody>
</table>
""".strip()


def _summary_chips(view: ReportView) -> str:
    s = view.summary
    chips: List[str] = [
        f'<span class="chip">total {s.total}</span>',
        f'<span class="chip green">ok {s.ok}</span>',
        f'<span class="chip {"red" if s.error else "green"}">error {s.error}</span>',
        f'<span class="chip">cache_hits {s.cache_hits}</span>',
        f'<span class="chip">hit_rate {s.hit_rate_pct}%</span>',
    ]
    return " ".join(chips)


def render(record: Mapping[str, Any], *, sentinel_keys: Iterable[str] | None = None) -> str:
    """Render a run-record dict as a single self-contained HTML document."""
    view = ReportView.from_record(record)
    sentinels = all_sentinels(record, sentinel_keys or [])

    sections: List[str] = []
    for step, cells in view.cells_by_step().items():
        sections.append(_step_section(step, cells, sentinels))

    body_sections = "\n".join(sections)

    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Audit Matrix Report</title>
<style>
{_INLINE_CSS}
</style>
</head>
<body>
<h1>Audit Matrix Report</h1>
<p class="meta">
  Generated <code>{_esc(view.generated_at)}</code> ·
  Tool <code>{_esc(view.tool)}</code> ·
  Code <code>{_esc(view.code_version)}</code> ·
  Config digest <code>{_esc(view.config_digest)}</code> ·
  Elapsed <code>{view.elapsed_seconds:.3f}s</code> ·
  Schema <code>v{SUPPORTED_SCHEMA_VERSION}</code>
</p>
<h2>Risk Summary</h2>
<p>{_summary_chips(view)}</p>
<p class="meta">redacted_key_ids: {", ".join(f"<code>{_esc(k)}</code>" for k in view.redacted_key_ids) or "(none)"}</p>
{body_sections}
<h2>Diff</h2>
<div class="diff" id="diff-placeholder">No baseline diff embedded. See <code>diff.md</code> in the same artifact bundle.</div>
<script>
{_INLINE_JS}
</script>
</body>
</html>
"""
    return html_doc


__all__ = ["render"]
