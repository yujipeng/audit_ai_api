"""Markdown rendering for audit-matrix run-record.json.

Output structure (reviewed for dual-distribution stylistic convergence with
``api_relay_audit/reporter.py``):

  # Audit Matrix Report
  **Generated** / **Tool** / **Code version** / **Config digest** / **Elapsed**

  ## Risk Summary
  - <icon> total/ok/error/cache_hits/hit_rate

  ## probe
  ### alpha (model: claude-opus-4-6) [<rating>]
  - status, latency, cache_hit, payload-summary
  ...

  ## Errors  (only when at least one cell errored)
  - probe/beta — TimeoutError: probe timed out

Icon set is intentionally identical to ``Reporter.flag``: 🔴 (red), 🟡 (yellow),
🟢 (green) — `tests/test_dual_distribution_parity.py` style invariant.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Iterable, List, Mapping, Sequence

from reporting.schema import (
    CellView,
    ReportView,
    SUPPORTED_SCHEMA_VERSION,
    all_sentinels,
)
from security.redact import redact_response_text, redact_text

RATING_ICON = {"red": "🔴", "yellow": "🟡", "green": "🟢"}

PAYLOAD_NOTE_MAX = 240  # truncate payload notes for readability


def _icon(rating: str) -> str:
    return RATING_ICON.get(rating, "⚪")


def _redact(s: str, sentinels: Sequence[str]) -> str:
    """Belt-and-suspenders: run text through both redactors.

    `redact_response_text` strips literal sentinel substrings (the only defense
    against payload-echoed credentials). `redact_text` then runs the canonical
    regex families. The order matters: sentinel stripping must happen before
    regex stripping so the regex can also catch obfuscated key fragments that
    survive the literal pass.
    """
    if not s:
        return s
    return redact_text(redact_response_text(s, set(sentinels)))


def _payload_summary(payload: Mapping[str, Any], sentinels: Sequence[str]) -> str:
    if not payload:
        return "(empty)"
    parts: List[str] = []
    score = payload.get("score")
    if score is not None:
        parts.append(f"score={score}")
    note = payload.get("note")
    if note:
        truncated = str(note)[:PAYLOAD_NOTE_MAX]
        parts.append(f"note={_redact(truncated, sentinels)!r}")
    raw = payload.get("raw_response")
    if raw:
        parts.append(f"raw={_redact(str(raw)[:PAYLOAD_NOTE_MAX], sentinels)!r}")
    extras = sorted(
        k for k in payload.keys() if k not in {"score", "note", "raw_response"}
    )
    if extras:
        parts.append(f"+keys={extras}")
    return ", ".join(parts) if parts else "(empty)"


def _error_line(cell: CellView, sentinels: Sequence[str]) -> str:
    err = cell.error or {}
    typ = err.get("type", "Error")
    msg = _redact(str(err.get("message", "")), sentinels)
    return f"- {cell.step}/{cell.endpoint} — {typ}: {msg}"


def _render_cell(cell: CellView, sentinels: Sequence[str]) -> str:
    head = (
        f"### {cell.endpoint} (model: {cell.model}) [{_icon(cell.rating)} {cell.rating}]\n"
    )
    bullets = [
        f"- status: `{cell.status}`",
        f"- latency: {cell.latency_ms:.1f} ms",
        f"- cache_hit: {str(cell.cache_hit).lower()}",
        f"- key: `{cell.redacted_key_id}`",
        f"- payload: {_payload_summary(cell.payload, sentinels)}",
    ]
    return head + "\n".join(bullets) + "\n"


def render(record: Mapping[str, Any], *, sentinel_keys: Iterable[str] | None = None) -> str:
    """Render a run-record dict as Markdown.

    `sentinel_keys` extends `record["redacted_key_ids"]` with caller-supplied
    sentinels (e.g. for fuzz testing). The combined set is passed to
    `redact_response_text` for every text field that may carry model-controlled
    content.
    """
    view = ReportView.from_record(record)
    sentinels = all_sentinels(record, sentinel_keys or [])

    out: List[str] = []
    out.append("# Audit Matrix Report\n")
    out.append("")
    out.append(f"**Generated**: {view.generated_at}")
    out.append(f"**Tool**: `{view.tool}`")
    out.append(f"**Code version**: `{view.code_version}`")
    out.append(f"**Config digest**: `{view.config_digest}`")
    out.append(f"**Elapsed**: {view.elapsed_seconds:.3f}s")
    out.append(f"**Schema**: v{SUPPORTED_SCHEMA_VERSION}")
    out.append("")

    out.append("## Risk Summary")
    out.append("")
    s = view.summary
    summary_icon = "🔴" if s.error else ("🟡" if s.hit_rate < 0.5 else "🟢")
    out.append(
        f"- {summary_icon} total={s.total} ok={s.ok} error={s.error} "
        f"cache_hits={s.cache_hits} cache_misses={s.cache_misses} "
        f"hit_rate={s.hit_rate_pct}%"
    )
    if view.redacted_key_ids:
        out.append(
            f"- 🔑 redacted_key_ids: "
            + ", ".join(f"`{k}`" for k in view.redacted_key_ids)
        )
    out.append("")

    for step, cells in view.cells_by_step().items():
        out.append(f"## {step}")
        out.append("")
        for cell in cells:
            out.append(_render_cell(cell, sentinels))

    errors = [c for c in view.cells if c.status == "error"]
    if errors:
        out.append("## Errors")
        out.append("")
        for cell in errors:
            out.append(_error_line(cell, sentinels))
        out.append("")

    return "\n".join(out)


def _cli(argv: Sequence[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <run-record.json>", file=sys.stderr)
        return 2
    record = json.loads(open(argv[1], encoding="utf-8").read())
    sys.stdout.write(render(record))
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli(sys.argv))


__all__ = ["render"]
