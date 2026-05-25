# models_diff fixtures

PM-curated fixtures for S1 probe-core / P3 `/v1/models` diff (TES-152).

## Files

| File | Purpose |
| --- | --- |
| `official_openai_catalog.json` | Human-curated top-27 OpenAI model IDs + `catalog_version` metadata |
| `official_anthropic_catalog.json` | Human-curated top-27 Anthropic model IDs + `catalog_version` metadata |
| `official_gemini_catalog.json` | Human-curated top-27 Gemini model IDs + `catalog_version` metadata |
| `case_normal_relay_listing.json` | Mock `/v1/models` response from a **healthy** relay (no diff vs official) |
| `case_inconsistent_relay_listing.json` | Mock `/v1/models` response from a **non-compliant** relay (extra + missing + suspicious aliases) |
| `suspicious_alias_counterexamples.json` | 8 hand-picked suspicious aliases + regex hints + rationale (F3) |

## Provenance

- **Curated by**: 资深产品经理 (PM agent) on 2026-05-25 to unblock TES-152 (F2+F3) per PJM dispatch on TES-93
- **Source**: Knowledge cutoff 2026-01 vendor docs + `api_relay_audit/perf/runner.py:VENDOR_MODELS` seed extension
- **Catalog totals**: 27 openai + 27 anthropic + 27 gemini = **81 model IDs** (PRD F2 floor: ≥80, ✅)
- **Review boundary**: human curation; **dev MUST cross-check against live vendor docs before merging Story-4** — fixture is a CI seed, not the ground truth. The `catalog_source` and `last_verified_at` fields in each catalog file mark this contract.

## How dev uses these

- Story-4 (`tests/test_probe_models_diff.py`) loads the three `official_*_catalog.json` as the reference set
- The two `case_*_relay_listing.json` feed `respx`-mocked `/v1/models` responses
- The expected diff outputs (`extra_in_relay` / `missing_in_relay` / `suspicious_aliases`) for `case_inconsistent` are listed in its `expected_diff` block — single source of truth, no need to recompute in test
- `suspicious_alias_counterexamples.json` provides regex seeds for the heuristic in `probe/models_diff.py` (PRD §3.1 P3 / §6.2 A6)

## Updating the catalog

`catalog_version` is a date string (`YYYY-MM-DD`). Bump when adding/removing model IDs. Dev should write `catalog_version` into every `ProbeReport.models_diff` so report consumers can audit which snapshot was in effect (PRD §4.3 + §7.1 R1).
