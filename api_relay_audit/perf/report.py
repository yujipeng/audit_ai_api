"""JSON + HTML report writers for the perf benchmark.

The HTML report is a single self-contained file that embeds the JSON data
inline (``window.__BENCH_DATA__``) and renders side-by-side comparison of
multiple endpoints / models with charts and tables. Open it directly in a
browser — no server required.
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime
from typing import Any


def write_json_report(result: dict, path: str) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def write_html_report(result: dict, path: str, *, title: str = "") -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    title = title or _default_title(result)
    payload = json.dumps(result, ensure_ascii=False)
    payload_safe = payload.replace("</", "<\\/")
    html_doc = _HTML_TEMPLATE.replace("{{TITLE}}", html.escape(title))\
        .replace("{{DATA}}", payload_safe)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_doc)


def _default_title(result: dict) -> str:
    eps = result.get("endpoints", [])
    names = [ep.get("name", "?") for ep in eps]
    when = result.get("generated_at", datetime.utcnow().isoformat())
    return f"API Relay Perf Comparison — {' vs '.join(names)} ({when[:16]})"


# ---------------------------------------------------------------------------
# HTML template — single file, vanilla JS, no external deps.
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{TITLE}}</title>
<style>
  :root {
    --bg: #0c0d10; --panel: #15171c; --border: #2a2e36;
    --fg: #e8e8e8; --muted: #9aa1ad; --accent: #4dd2ff;
    --green: #4cd28a; --yellow: #f3c969; --red: #ff6b6b;
    --grey: #5a606e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px 28px 60px;
    background: var(--bg); color: var(--fg);
    font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
                  "PingFang SC", "Helvetica Neue", Arial, sans-serif;
  }
  h1 { font-size: 22px; margin: 0 0 4px; }
  h2 { font-size: 16px; margin: 24px 0 12px; color: var(--accent); }
  h3 { font-size: 14px; margin: 18px 0 8px; }
  .meta { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 18px; margin-bottom: 18px;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td {
    padding: 6px 10px; border-bottom: 1px solid var(--border);
    text-align: left; vertical-align: top;
  }
  th { color: var(--muted); font-weight: 500; }
  tr:last-child td { border-bottom: none; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; }
  .pill {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600; letter-spacing: .02em;
  }
  .pill.clean      { background: rgba(76,210,138,.16); color: var(--green); }
  .pill.suspicious { background: rgba(243,201,105,.18); color: var(--yellow); }
  .pill.injected   { background: rgba(255,107,107,.20); color: var(--red); }
  .pill.failed     { background: rgba(90,96,110,.30); color: var(--muted); }
  .pill.unknown    { background: rgba(90,96,110,.30); color: var(--muted); }
  .err { color: var(--red); font-family: ui-monospace, SFMono-Regular,
         Menlo, monospace; font-size: 12px; word-break: break-all; }
  details { margin-top: 6px; }
  details summary { cursor: pointer; color: var(--muted); }
  details pre {
    background: #0a0a0c; border: 1px solid var(--border);
    padding: 10px 12px; border-radius: 6px;
    white-space: pre-wrap; word-wrap: break-word;
    font: 12px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    color: #d6d6d6; max-height: 380px; overflow: auto;
  }
  .grid {
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  }
  .stat .v { font-size: 22px; font-weight: 600; }
  .stat .l { color: var(--muted); font-size: 11px; text-transform: uppercase;
             letter-spacing: .08em; margin-top: 4px; }
  .endpoint-head {
    display: flex; align-items: baseline; gap: 12px;
    flex-wrap: wrap; margin-bottom: 6px;
  }
  .endpoint-head .url { color: var(--muted); font-family: ui-monospace,
                         SFMono-Regular, Menlo, monospace; font-size: 12px; }
  .bar-wrap { background: #1d2027; border-radius: 4px; height: 8px;
              overflow: hidden; margin-top: 4px; }
  .bar { height: 100%; }
  .bar.green { background: var(--green); }
  .bar.yellow { background: var(--yellow); }
  .bar.red { background: var(--red); }
  .legend { color: var(--muted); font-size: 12px; }
  .small { color: var(--muted); font-size: 12px; }
  .nav { display: flex; gap: 16px; margin: 12px 0 22px; flex-wrap: wrap; }
  .nav a { color: var(--accent); text-decoration: none; font-size: 13px; }
  .nav a:hover { text-decoration: underline; }
  .ok { color: var(--green); }
  .bad { color: var(--red); }
  .warn { color: var(--yellow); }
  .footnote { color: var(--muted); font-size: 11px; margin-top: 28px; }
  .col-flex { display: flex; gap: 18px; flex-wrap: wrap; }
  .col-flex > .panel { flex: 1; min-width: 320px; }
</style>
</head>
<body>
<h1>{{TITLE}}</h1>
<div class="meta" id="meta"></div>

<div class="nav" id="nav"></div>

<div id="overview"></div>
<div id="comparison"></div>
<div id="endpoints"></div>

<div class="footnote">
  Generated by <code>scripts/perf-bench.py</code> &middot;
  api-relay-audit perf extension &middot; data is embedded inline in this file.
</div>

<script>
window.__BENCH_DATA__ = {{DATA}};
</script>

<script>
(function () {
  const data = window.__BENCH_DATA__;
  const $ = (id) => document.getElementById(id);

  const fmt = {
    secs(v) {
      if (v == null) return "—";
      if (v < 1) return (v * 1000).toFixed(0) + " ms";
      return v.toFixed(2) + " s";
    },
    pct(v) { return v == null ? "—" : (v * 100).toFixed(1) + "%"; },
    int(v) { return v == null ? "—" : Number(v).toLocaleString(); },
    ratio(num, den) {
      if (!den) return "—";
      return ((num / den) * 100).toFixed(0) + "%";
    },
    text(v, max) {
      max = max || 220;
      if (!v) return "";
      v = String(v);
      return v.length > max ? v.slice(0, max) + "…" : v;
    },
  };

  function pill(verdict) {
    const v = verdict || "unknown";
    return `<span class="pill ${v}">${v.toUpperCase()}</span>`;
  }

  // ----- meta block -------------------------------------------------------
  const meta = $("meta");
  const t = data.test || {};
  meta.innerHTML = [
    `Generated: <b>${data.generated_at || "?"}</b>`,
    `Total elapsed: <b>${fmt.secs(data.elapsed_seconds)}</b>`,
    `Prompt: <code>${(t.prompts && t.prompts[0]) || t.prompt || ""}</code>`,
    `Rounds: <b>${t.rounds}</b>`,
    `Concurrency: <b>${t.concurrency}</b>`,
    `Timeout: <b>${t.timeout}s</b>`,
    `Max tokens: <b>${t.max_tokens}</b>`,
  ].map(s => `<span style="margin-right:18px">${s}</span>`).join("");

  // ----- nav --------------------------------------------------------------
  const nav = $("nav");
  nav.innerHTML = [
    `<a href="#overview-section">Overview</a>`,
    `<a href="#comparison-section">Comparison</a>`,
    ...data.endpoints.map((ep, i) =>
      `<a href="#endpoint-${i}">${ep.name}</a>`)
  ].join("");

  // ----- overview ---------------------------------------------------------
  const allEndpoints = data.endpoints || [];
  let totalRounds = 0, okRounds = 0, errRounds = 0, modelCount = 0;
  for (const ep of allEndpoints) {
    for (const r of ep.results) {
      modelCount += 1;
      const m = r.metrics || {};
      const succ = m.successful_rounds || 0;
      const fail = m.failed_rounds || 0;
      okRounds += succ;
      errRounds += fail;
      totalRounds += succ + fail;
    }
  }
  $("overview").innerHTML = `
    <h2 id="overview-section">Overview</h2>
    <div class="panel grid">
      <div class="stat"><div class="v">${allEndpoints.length}</div>
        <div class="l">Endpoints tested</div></div>
      <div class="stat"><div class="v">${modelCount}</div>
        <div class="l">Endpoint × Model bench rows</div></div>
      <div class="stat"><div class="v">${totalRounds}</div>
        <div class="l">Total rounds</div></div>
      <div class="stat"><div class="v">${fmt.ratio(okRounds, totalRounds)}</div>
        <div class="l">Overall success rate</div></div>
      <div class="stat"><div class="v ${errRounds ? "bad" : "ok"}">${errRounds}</div>
        <div class="l">Failed rounds</div></div>
    </div>`;

  // ----- comparison table ------------------------------------------------
  const compRows = [];
  for (const ep of allEndpoints) {
    for (const r of ep.results) {
      const m = r.metrics || {};
      const ttft = m.ttft_seconds || {};
      const tot = m.total_seconds || {};
      const purity = r.purity || {};
      compRows.push({
        endpoint: ep.name, model: r.model,
        succ: m.successful_rounds || 0,
        fail: m.failed_rounds || 0,
        rate: m.success_rate,
        ttftAvg: ttft.avg, ttftP95: ttft.p95, ttftP99: ttft.p99,
        totAvg: tot.avg, totP95: tot.p95, totP99: tot.p99,
        verdict: purity.verdict || "unknown",
      });
    }
  }
  const compTable = compRows.map(r => `
    <tr>
      <td>${r.endpoint}</td>
      <td><code>${r.model}</code></td>
      <td class="num">${r.succ}/${r.succ + r.fail}</td>
      <td class="num">${fmt.secs(r.ttftAvg)}</td>
      <td class="num">${fmt.secs(r.ttftP95)}</td>
      <td class="num">${fmt.secs(r.ttftP99)}</td>
      <td class="num">${fmt.secs(r.totAvg)}</td>
      <td class="num">${fmt.secs(r.totP95)}</td>
      <td class="num">${fmt.secs(r.totP99)}</td>
      <td>${pill(r.verdict)}</td>
    </tr>`).join("");
  $("comparison").innerHTML = `
    <h2 id="comparison-section">Side-by-side comparison</h2>
    <div class="panel">
      <table>
        <thead><tr>
          <th>Endpoint</th><th>Model</th><th>OK / Total</th>
          <th>TTFT avg</th><th>TTFT p95</th><th>TTFT p99</th>
          <th>Total avg</th><th>Total p95</th><th>Total p99</th>
          <th>Purity</th>
        </tr></thead>
        <tbody>${compTable || `<tr><td colspan="10" class="small">No data</td></tr>`}</tbody>
      </table>
      <div class="legend" style="margin-top:10px">
        <b>TTFT</b> = time to first token (streaming).
        <b>Total</b> = total wall-clock time of the streamed response.
        <b>Purity</b>:
        ${pill("clean")} no injection markers detected,
        ${pill("suspicious")} unusual patterns,
        ${pill("injected")} prompt-injection / identity leak detected,
        ${pill("failed")} every round errored.
      </div>
    </div>`;

  // ----- per endpoint -----------------------------------------------------
  const epHtml = allEndpoints.map((ep, i) => {
    const blocks = ep.results.map((r, j) => modelBlock(ep, r, i, j)).join("");
    const listed = (ep.models_listed || []).slice(0, 80);
    const listedStr = listed.length
      ? listed.map(x => `<code>${x}</code>`).join(", ")
      : `<span class="small">/v1/models unavailable: ${ep.models_listed_error || "no data"}</span>`;
    return `
      <h2 id="endpoint-${i}">Endpoint: ${ep.name}</h2>
      <div class="panel">
        <div class="endpoint-head">
          <div><b>${ep.name}</b></div>
          <div class="url">${ep.base_url}</div>
          <div class="small">format: ${ep.format}${ep.vendor ? ` &middot; vendor: <b>${ep.vendor}</b>` : ""}</div>
        </div>
        <div class="small" style="margin-top:6px">
          <b>Models advertised by /v1/models</b>: ${listedStr}
          ${listed.length === 80 ? "… (truncated)" : ""}
        </div>
      </div>
      ${blocks}
    `;
  }).join("");
  $("endpoints").innerHTML = epHtml;

  function modelBlock(ep, r, epIdx, modelIdx) {
    const m = r.metrics || {};
    const purity = r.purity || {};
    const ttft = m.ttft_seconds || {};
    const tot = m.total_seconds || {};
    const out = m.output_chars || {};

    const errs = (r.errors || []).filter(Boolean);
    const errPanel = errs.length
      ? `<div class="panel" style="margin-top:8px">
           <div class="warn"><b>${errs.length} failed rounds</b></div>
           <ul>${errs.slice(0, 8).map(e => `<li class="err">${escapeHtml(e)}</li>`).join("")}</ul>
           ${errs.length > 8 ? `<div class="small">… and ${errs.length - 8} more</div>` : ""}
         </div>`
      : "";

    const issueRows = Object.entries(purity.issue_counts || {})
      .map(([k, v]) => `<li><b>${k}</b>: ${v}</li>`).join("")
      || `<li class="small">no purity issues</li>`;

    const idRows = Object.entries(purity.identities_seen || {})
      .map(([k, v]) => `<li><b>${k}</b> seen ${v}×</li>`).join("");

    const roundRows = (r.rounds || []).map(rd => `
      <tr>
        <td class="num">${rd.round}</td>
        <td>${rd.ok ? `<span class="ok">OK</span>` : `<span class="bad">ERR</span>`}</td>
        <td class="num">${fmt.secs(rd.ttft_seconds)}</td>
        <td class="num">${fmt.secs(rd.total_seconds)}</td>
        <td class="num">${fmt.int(rd.output_chars)}</td>
        <td class="num">${rd.chunk_count}</td>
        <td>${rd.finish_reason || ""}</td>
        <td>${(rd.issues || []).map(i => `<span class="pill suspicious">${i}</span>`).join(" ")}</td>
        <td>
          ${rd.error ? `<span class="err">${escapeHtml(fmt.text(rd.error, 200))}</span>` : ""}
          ${rd.text_preview ? `<details><summary>preview</summary><pre>${escapeHtml(rd.text_preview)}</pre></details>` : ""}
        </td>
      </tr>`).join("");

    return `
      <div class="panel" id="endpoint-${epIdx}-model-${modelIdx}">
        <h3><code>${r.model}</code> ${pill(purity.verdict)}
          <span class="small">${purity.verdict_reason || ""}</span></h3>

        <div class="grid">
          <div class="stat"><div class="v">${m.successful_rounds || 0}/${(m.successful_rounds||0)+(m.failed_rounds||0)}</div>
            <div class="l">Successful rounds</div></div>
          <div class="stat"><div class="v">${fmt.secs(ttft.avg)}</div>
            <div class="l">TTFT avg</div></div>
          <div class="stat"><div class="v">${fmt.secs(ttft.p95)}</div>
            <div class="l">TTFT p95</div></div>
          <div class="stat"><div class="v">${fmt.secs(ttft.p99)}</div>
            <div class="l">TTFT p99</div></div>
          <div class="stat"><div class="v">${fmt.secs(tot.avg)}</div>
            <div class="l">Total avg</div></div>
          <div class="stat"><div class="v">${fmt.secs(tot.p95)}</div>
            <div class="l">Total p95</div></div>
          <div class="stat"><div class="v">${fmt.secs(tot.p99)}</div>
            <div class="l">Total p99</div></div>
          <div class="stat"><div class="v">${fmt.int(out.avg && Math.round(out.avg))}</div>
            <div class="l">Avg output chars</div></div>
        </div>

        <div class="col-flex" style="margin-top:14px">
          <div class="panel" style="background:#10131a">
            <div class="small">Latency distribution (s)</div>
            <table>
              <thead><tr><th></th><th>min</th><th>avg</th><th>p50</th>
                <th>p90</th><th>p95</th><th>p99</th><th>max</th><th>stdev</th></tr></thead>
              <tbody>
                <tr><td>TTFT</td>
                  <td class="num">${fmt.secs(ttft.min)}</td>
                  <td class="num">${fmt.secs(ttft.avg)}</td>
                  <td class="num">${fmt.secs(ttft.p50)}</td>
                  <td class="num">${fmt.secs(ttft.p90)}</td>
                  <td class="num">${fmt.secs(ttft.p95)}</td>
                  <td class="num">${fmt.secs(ttft.p99)}</td>
                  <td class="num">${fmt.secs(ttft.max)}</td>
                  <td class="num">${fmt.secs(ttft.stdev)}</td></tr>
                <tr><td>Total</td>
                  <td class="num">${fmt.secs(tot.min)}</td>
                  <td class="num">${fmt.secs(tot.avg)}</td>
                  <td class="num">${fmt.secs(tot.p50)}</td>
                  <td class="num">${fmt.secs(tot.p90)}</td>
                  <td class="num">${fmt.secs(tot.p95)}</td>
                  <td class="num">${fmt.secs(tot.p99)}</td>
                  <td class="num">${fmt.secs(tot.max)}</td>
                  <td class="num">${fmt.secs(tot.stdev)}</td></tr>
              </tbody>
            </table>
          </div>
          <div class="panel" style="background:#10131a">
            <div class="small">Purity</div>
            <p>Verdict: ${pill(purity.verdict)} <span class="small">${purity.verdict_reason || ""}</span></p>
            <p>Clean rounds: <b>${purity.rounds_clean}</b> / ${purity.rounds_total}
               (ok: ${purity.rounds_ok}, refusals: ${purity.refusal_count},
               empty: ${purity.empty_count}, language-mismatch: ${purity.language_mismatch_count})</p>
            <ul>${issueRows}</ul>
            ${idRows ? `<p class="warn">Identity leakage:</p><ul>${idRows}</ul>` : ""}
          </div>
        </div>

        ${errPanel}

        <details style="margin-top:10px">
          <summary>Per-round detail (${(r.rounds || []).length})</summary>
          <table>
            <thead><tr>
              <th>#</th><th>status</th><th>ttft</th><th>total</th>
              <th>chars</th><th>chunks</th><th>finish</th>
              <th>issues</th><th>response / error</th>
            </tr></thead>
            <tbody>${roundRows}</tbody>
          </table>
        </details>
      </div>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;",
      "\"": "&quot;", "'": "&#39;"
    }[c]));
  }
})();
</script>
</body>
</html>
"""
