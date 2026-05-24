# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**Dual-purpose AI API relay/proxy testing tool** combining security audit and performance benchmarking:

1. **Security Audit** (upstream `api-relay-audit`): 13-step detection of hidden prompt injection, prompt leakage, instruction override with non-Claude identity substitution, context truncation, tool-call package substitution (AC-1.a), error response header leakage (AC-2 adjacent), SSE-level stream integrity anomalies (AC-1 SSE-layer), Web3 prompt injection (SlowMist signature isolation, profile-gated), relay-framework fingerprinting, and latency-variance fingerprinting.

2. **Performance Benchmark** (new `perf-bench`): Streaming performance testing (TTFT, p50/p90/p95/p99 latency) + purity validation (identity drift, [REDACTED] leakage, relay internal tokens, language mismatch, refusal, empty response) in a single test run. Supports multi-endpoint comparison with vendor presets (`gpt`, `claude`) and YAML-based configuration.

Threat taxonomy follows Liu et al., *Your Agent Is Mine*, arXiv:2604.08407 — AC-1 (payload injection), AC-1.a (dependency-targeted injection), AC-1.b (conditional delivery), AC-2 (secret exfiltration). Infrastructure fingerprint (Step 12) and latency variance (Step 13) are sourced from Zhang et al., *Real Money, Fake Models*, arXiv:2603.01919. AC-1 full tool_call support and AC-1.b beyond warm-up mitigation remain on the backlog (see FOR_JOHN.md).

## Scope / Constraints

**Editable without asking**: `scripts/`, `api_relay_audit/`, `tests/`, `audit.py` (root standalone), `ROADMAP.md`, `CLAUDE.md`, `FOR_JOHN.md`.

**Ask before touching**: `web/`, `.github/workflows/`, `.github/voice-samples/`, `docs/`, `deploy/`, any root-level config files.

**Why**: `web/` is under a frontend colleague handoff (post-2026-04-20). `.github/workflows/` contains Claude Code action configuration — changes there have external side effects.

## Contribution Philosophy

**User-feedback-driven, not speculative.** Do not add features because they might be useful — add them when a real user need has been reported. This applies to code changes, new detection steps, and incoming PRs.

**Permanently out of scope** (evaluated and deliberately dropped — do not re-open without new information):
- **Claude Code CLI header impersonation** (ROADMAP §14): brittle version-pinning, and impersonating CC headers removes audit differentiation value
- **Hosted web dashboard** (ROADMAP "Explicitly NOT doing"): requires API backend + auth, which changes the product from a one-curl-download tool to a hosted service

**PR evaluation heuristics**: Does the change address a reported user problem? Does it preserve the dual-distribution invariant? Does it add complexity for a use case with zero user reports?

## Commands

```bash
# Install dependencies
pip install httpx pytest

# === Security Audit (13-step) ===
# Run full audit
python scripts/audit.py --key <KEY> --url <BASE_URL> --model [REDACTED]

# Context length test only
python scripts/context-test.py --key <KEY> --url <BASE_URL>

# === Performance Benchmark ===
# Single endpoint with vendor preset (auto-expands to model family)
python scripts/perf-bench.py <URL> <KEY> gpt --rounds 10 --output reports/test.html
python scripts/perf-bench.py <URL> <KEY> claude --rounds 10 --output reports/test.html

# Multi-endpoint comparison via YAML config
python scripts/perf-bench.py --config perf-configs/example.yaml --output reports/comparison.html

# Custom prompt and models
python scripts/perf-bench.py <URL> <KEY> gpt --prompt "讲一个笑话" --rounds 5
python scripts/perf-bench.py <URL> <KEY> gpt --model gpt-4o-mini --model o1-mini --rounds 10

# Concurrent testing
python scripts/perf-bench.py <URL> <KEY> gpt --rounds 30 --concurrency 5

# === Utilities ===
# Extract report data to JSON (for dashboard)
python scripts/extract-data.py --reports-dir ./reports --output data.json

# Doc-drift prevention — run before publishing any external comparison/blog/X long-form
python scripts/collect-metrics.py

# === Testing ===
# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_client.py -v

# Run a single test case
python -m pytest tests/test_client.py::TestAutoDetection::test_format_cached -v
```

## Vendor Presets

Performance benchmark supports vendor shortcuts that auto-expand to model families:

| Vendor   | Expands to                                                    |
|----------|---------------------------------------------------------------|
| `gpt`    | `gpt-5.2`, `gpt-5.3-codex`, `gpt-5.5`                         |
| `claude` | `[REDACTED]`, `[REDACTED]`, `[REDACTED]`    |

Override with explicit `--model` flags or `models:` in YAML config.

## Doc-drift prevention

Before publishing any external comparison / blog / X long-form post that quotes step counts, test counts, version numbers, or Codex review tallies, run `python scripts/collect-metrics.py`. The script regenerates `docs/_metrics.md` (committed, GitHub-readable) and `docs/_metrics.json` (gitignored, machine-readable). Verify every numeric claim in your draft against the table.

Coverage contract: ~70% of typical drift (structured metrics — version, step count, test count, CLI flag count, Codex review tallies, ROADMAP progression, dual-distribution version parity). The remaining ~30% (external competitor intel, narrative completeness, framing) is documented as the human-review boundary inside `_metrics.md` itself.

Origin: 21-day drift in `docs/comparison-api-relay-audit-vs-hvoy-vs-cctest.md` caught while preparing X publication, 2026-05-05. Pareto-frontier selection — introspective generation chosen over template rendering (overkill for a single-author monthly-release project) and over manual discipline (already failed once).

## Architecture

### Dual Distribution Model
There are **two parallel versions**:
- `audit.py` (root) — standalone, zero-dependency version (~2500 lines, curl-only). Users can `curl` this file and run it without installing anything.
- `api_relay_audit/` + `scripts/` — modular version with `httpx`, used for development and testing.

When making changes to audit logic, `audit.py` (root) must be updated to stay in sync. `tests/test_dual_distribution_parity.py::test_risk_matrix_character_identical` enforces byte-level parity on the risk-matrix block; `tests/test_web3_injection.py::TestWeb3MarkerParity` enforces it on Web3 markers; `tests/test_refusal_detector.py::TestRefusalMarkerParity` enforces it on the Step 4/6 refusal vocabulary.

### Module Responsibilities

**Security Audit Modules** (`api_relay_audit/`):
- `client.py` — All API calls go through `APIClient`. Auto-detects Anthropic vs OpenAI format (tries Anthropic first, caches on success). On SSL errors switches httpx → subprocess curl (`-sk`).
- `context.py` — Canary-marker + binary search context truncation. Coarse scan → binary → fine, ~12 requests vs ~75 naive.
- `reporter.py` — Builder-pattern Markdown report. `flag(level, msg)` appends to both body and risk summary.
- `tool_substitution.py` — AC-1.a via text-echo of pinned package commands (`pip install requests==2.31.0`, etc.). Text surrogate only: does NOT catch rewrites targeting structured `tool_call` payloads.
- `error_leakage.py` — AC-2 adjacent. 7-8 deterministic broken requests (malformed JSON, invalid model, wrong content-type, missing fields, unknown endpoint, `max_tokens=99999999` force-upstream, fake Bearer auth probe). Three scan paths: literal key match, LiteLLM-ported regex, LiteLLM issue-sourced markers (#5762, #8075, #12152, #13705, #15799, #20419).
- `identity_patterns.py` — 26 non-Claude keywords. ASCII uses word-bounded regex (`Qwen2.5` matches, `laws` doesn't); CJK uses substring.
- `stream_integrity.py` — SSE whitelist + usage monotonicity + thinking signature + stream model identity check. Tri-state verdict (`clean`/`anomaly`/`inconclusive`). Clean-room reimplementation of hvoy.ai concept, not a port.
- `transparent_log.py` — Append-only JSONL forensic log. Hash-only (no body), entries ≤1.5 KB. Hooks into all 4 `APIClient` public methods (`call`, `get_models`, `raw_request`, `stream_call`).
- `web3/injection_probes.py` — 3 SlowMist-derived probes; safe-priority aggregation with `HARD_INJECTED_MARKERS` override for contradictory responses. Profile-gated (`--profile web3|full`).
- `infra_fingerprint.py` — 3 unauthenticated GET probes; signature DB covers 7 frameworks; majority vote → `confirmed`/`tentative`/`unknown`. Informational only, does not feed the risk matrix.
- `latency_variance.py` — N identical `max_tokens=8` requests timed with `time.perf_counter` (not `time.time` — monotonicity, v1.8.1 fix). `ensure_format()` is called before the timing loop to prevent the first sample silently including a failed Anthropic probe. Bimodality is the strong signal for silent A/B model substitution. Informational only.

**Performance Benchmark Modules** (`api_relay_audit/perf/`):
- `streaming.py` — SSE streaming client with TTFT (time-to-first-token) measurement. Handles both Anthropic and OpenAI streaming formats.
- `metrics.py` — Statistical aggregation: mean, median, p50/p90/p95/p99, standard deviation.
- `purity.py` — 6 heuristic detectors run on each streaming response: `identity_leak` (non-target model self-identification), `system_prompt_leak` (template markers like `[INST]`, `<|im_start|>`), `relay_internal_token` (leaked credentials/internal strings), `language_mismatch` (Chinese prompt → English response), `refusal` (benign prompt triggers refusal), `empty_response` (HTTP 200 but empty body).
- `runner.py` — Test matrix orchestration. Parses YAML config, expands vendor presets, runs concurrent batches, aggregates results.
- `report.py` — Generates single-file HTML report (with embedded CSS/JS for visualization) and JSON output. Includes per-endpoint verdict (`clean`/`suspicious`/`injected`/`error`) based on purity detector aggregation.

**Orchestration**:
- `scripts/audit.py` — 13-step security audit orchestration. **6D risk matrix**: D1=token injection, D2=instruction override, D3=tool-call substitution, D4=error leakage, D5=stream anomaly, D6=Web3 injection (profile-gated). Steps 12/13 informational only. `--profile` gates step set at runtime — rejected branch-forking to preserve the dual-distribution invariant.
- `scripts/perf-bench.py` — Performance benchmark CLI entry point. Supports positional args (single endpoint) or `--config` (YAML multi-endpoint).

### APIClient Return Format
```python
{"text": str, "input_tokens": int, "output_tokens": int, "raw": dict, "time": float}
# or on error:
{"error": str}
```

## CLI Flags

**Security Audit** (`scripts/audit.py`):
`--key`, `--url`, `--model`, `--output`, `--profile {general,web3,full}`, `--skip-infra`, `--skip-context`, `--skip-tool-substitution`, `--skip-error-leakage`, `--aggressive-error-probes`, `--skip-stream-integrity`, `--skip-web3-injection`, `--skip-infra-fingerprint`, `--skip-latency-variance`, `--latency-probe-count N`, `--warmup N`, `--timeout`

**Performance Benchmark** (`scripts/perf-bench.py`):
Positional: `<url> <key> <vendor>` OR `--config <yaml>`
Optional: `--rounds N`, `--concurrency N`, `--prompt TEXT`, `--prompts-file PATH`, `--model MODEL` (repeatable), `--output PATH`, `--timeout N`, `--max-tokens N`, `--format {openai,anthropic}`, `--temperature FLOAT`

## Dual-distribution invariant
Whenever `scripts/audit.py` or any `api_relay_audit/*.py` module changes, the standalone `audit.py` at the repo root must be updated to match. The standalone version is a character-copy of the modular code with curl subprocess replacing httpx. New helper modules (e.g. `tool_substitution.py`) get inlined as a new `Section` block in `audit.py`.

## Reference Documents

- `FOR_JOHN.md` — architecture decisions, design pitfalls, and "why we didn't do X" reasoning. Read before making structural changes.
- `ROADMAP.md` — near-term candidates (§2), deferred (§2.4/2.45), and permanently out-of-scope (§2.6 "Explicitly NOT doing"). Check before implementing any new feature.
- `.github/voice-samples/` — tone and structure for issue replies (`pr-reply-sample.md`) and PR reviews (`pr-review-sample.md`). The automated `claude-issue-triage.yml` and `claude-pr-review.yml` workflows read these files.
- `README.md` — User-facing documentation for the performance benchmark feature (Chinese).
- `README-upstream-audit.md` — Original upstream documentation for the 13-step security audit.
