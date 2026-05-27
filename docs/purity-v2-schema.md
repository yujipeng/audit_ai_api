# Purity v2.0 Report Schema (TES-165 / S2-4)

> Status: stable contract for the `purity-v2.0` JSON envelope produced by
> `scripts/purity-eval.py` and `api_relay_audit.evaluator.reporter`.
>
> Source of truth: `api_relay_audit.evaluator.aggregator.PurityReportV2` +
> `api_relay_audit.evaluator.reporter.report_to_dict(...)`.

## 1. Top-level envelope

| Field | Type | Description |
| --- | --- | --- |
| `schema_version` | `string` | Locked literal `"purity-v2.0"` (design §8). Bumping requires a new schema doc. |
| `metadata` | `object` | Run-level context; see §2. |
| `dimensions` | `object<DimensionId, DimensionResult>` | One entry per evaluator that ran; see §3. |
| `overall` | `object` | Aggregated verdict + reason; see §4. |

## 2. `metadata`

| Field | Type | Description |
| --- | --- | --- |
| `target_url` | `string` | Relay base URL. |
| `api_key_fingerprint` | `string` | Last-4-char fingerprint, e.g. `sk-...abcd`. The full key is **never** serialised. |
| `vendor` | `"gpt"` \| `"claude"` | Vendor format selected for the run. |
| `profile` | `string` | Currently always `"default"`; baseline support is deferred to P1. |
| `started_at` | `string (ISO-8601 UTC)` | Wall-clock run start. **Excluded from AC-R3 replay diff.** |
| `duration_s` | `number` | Aggregated evaluator duration. **Excluded from AC-R3 replay diff.** |
| `rounds_requested` | `integer` | `--rounds` flag value. |
| `evaluators_run` | `array<DimensionId>` | Insertion-order list of evaluator dimensions. **Excluded from AC-R3 replay diff** (set-equality only). |

## 3. `dimensions[DimensionId]`

`DimensionId` is a closed Literal — see §6.

| Field | Type | Description |
| --- | --- | --- |
| `dimension` | `DimensionId` | Echoes the key, kept for downstream tools that flatten to a list. |
| `verdict` | `Verdict` | One of `clean / suspicious / injected / failed / unknown`. |
| `verdict_reason` | `string` | Human-readable rationale; safe to log verbatim (no secret bleed). |
| `rounds_total` | `integer` | Number of probe rounds requested. |
| `rounds_ok` | `integer` | Rounds that returned a usable response (no transport error). |
| `hits` | `array<object>` | Per-round forensic detail. Snippets are redacted via `redact_snippet`. |
| `duration_s` | `number` | Wall-clock seconds spent inside this evaluator. **Excluded from AC-R3 replay diff.** |
| `raw_artifacts` | `array<object> \| null` | Full raw round-trip records. Present **only when `--raw-keep` is passed** (AC-F5). When stripped, the field itself is absent from the JSON output. |

### 3.1 `hits[]` shape

`hits` is an open dict-of-strings shape; the most common keys are:

- `round` — integer index into the probe sequence
- `issue` / `identity` / `kind` / `manager` — per-dimension category
- `severity` — `"high" | "medium" | "low"`
- `snippet` — redacted excerpt (≤ 200 chars in HTML render)

## 4. `overall`

| Field | Type | Description |
| --- | --- | --- |
| `verdict` | `Verdict` | Worst-of-children (PRD §4.6 rank: `injected > suspicious > failed > unknown > clean`). |
| `verdict_reason` | `string` | E.g. `"injected: swap, leak"`. |
| `confidence` | `number (0..1)` | `sum(rounds_ok) / sum(rounds_total)` across dimensions. **Does not** influence verdict ranking — it is a reliability hint only. |
| `worst_dimensions` | `array<DimensionId>` | All dimensions tied at the worst rank. |

## 5. `Verdict` literal lock (§8 design)

Closed set. Adding a new value requires a `purity-v2.x` schema bump.

| Verdict | Meaning |
| --- | --- |
| `clean` | No injection / substitution markers; default safe outcome. |
| `suspicious` | Minor anomaly under threshold (e.g. refusal rate just above the suspicious cutoff). |
| `injected` | Confirmed AC-1 / AC-1.a / AC-2 marker — actionable signal. |
| `failed` | Every probe round errored out (no signal extractable). |
| `unknown` | Either the dimension is a placeholder (e.g. drift in S2-4) or every probe was inconclusive. |

## 6. `DimensionId` literal lock (§8 design)

Closed set. Adding a new value requires both a new evaluator + a `purity-v2.x` schema bump.

```
injection
swap
drift
tool
refusal
leak
web3_injection   (default-off, opt-in via --include)
```

## 7. `raw_artifacts` policy (AC-F5)

- **Default (`--raw-keep` absent):** the `raw_artifacts` field is **omitted** for every dimension. This keeps a 6-dimension clean report ≤ 50 KB (AC-N3).
- **`--raw-keep` set:** each dimension carries a `raw_artifacts` array (or `null` if the evaluator collected none). Round-trip records are passed through verbatim from the per-evaluator collector — they may include full prompts and full responses, which can contain user content. Treat the file as sensitive.

## 8. Replay determinism (AC-R3)

`render_json` calls `json.dumps(..., sort_keys=True)`. Two runs against the same input (or same stub) produce byte-identical JSON **after** removing the timestamp/duration fields enumerated above.

The CLI test in `tests/test_purity_cli.py::TestCLIEndToEndStub::test_replay_idempotent_ac_r3` enforces this on the stub path; the integration test in `tests/test_evaluator_pipeline.py::TestPipelineDeterminismAcR3` enforces the same property end-to-end against `FakeHonestClient`.

## 9. Exit codes (CLI / AC-X1-3)

| Verdict | default | `--strict` |
| --- | --- | --- |
| `clean` | 0 | 0 |
| `injected` \| `failed` | 1 | 1 |
| `suspicious` \| `unknown` | 0 | 2 |

The mapping lives in `_compute_exit_code(verdict, strict)` inside `scripts/purity-eval.py`. Any change to the table must update both this doc and `tests/test_purity_cli.py::TestExitCodeFunction`.
