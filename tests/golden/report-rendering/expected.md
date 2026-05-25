# Audit Matrix Report


**Generated**: 2026-05-25T11:00:00+00:00
**Tool**: `audit-matrix`
**Code version**: `v1.9.0-dev`
**Config digest**: `cfg-deadbeef`
**Elapsed**: 12.345s
**Schema**: v1

## Risk Summary

- 🔴 total=8 ok=7 error=1 cache_hits=1 cache_misses=7 hit_rate=12.5%
- 🔑 redacted_key_ids: `aaaa1111`, `bbbb2222`

## probe

### alpha (model: claude-opus-4-6) [🟢 green]
- status: `ok`
- latency: 110.0 ms
- cache_hit: false
- key: `aaaa1111`
- payload: score=0.92, note='probe-alpha-ok'

### beta (model: claude-opus-4-6) [🔴 red]
- status: `error`
- latency: 35.0 ms
- cache_hit: false
- key: `bbbb2222`
- payload: (empty)

## purity

### alpha (model: claude-opus-4-6) [🟢 green]
- status: `ok`
- latency: 200.0 ms
- cache_hit: false
- key: `aaaa1111`
- payload: score=0.81, note='purity-alpha-ok'

### beta (model: claude-opus-4-6) [🟢 green]
- status: `ok`
- latency: 215.0 ms
- cache_hit: false
- key: `bbbb2222`
- payload: score=0.78, note='purity-beta-ok'

## perf

### alpha (model: claude-opus-4-6) [🟢 green]
- status: `ok`
- latency: 500.0 ms
- cache_hit: false
- key: `aaaa1111`
- payload: score=0.95, note='perf-alpha'

### beta (model: claude-opus-4-6) [🟢 green]
- status: `ok`
- latency: 520.0 ms
- cache_hit: false
- key: `bbbb2222`
- payload: score=0.93, note='perf-beta'

## pricing

### alpha (model: claude-opus-4-6) [🟢 green]
- status: `ok`
- latency: 75.0 ms
- cache_hit: true
- key: `aaaa1111`
- payload: score=0.88, note='pricing-alpha-cached'

### beta (model: claude-opus-4-6) [🟢 green]
- status: `ok`
- latency: 90.0 ms
- cache_hit: false
- key: `bbbb2222`
- payload: score=0.85, note='pricing-beta'

## Errors

- probe/beta — TimeoutError: probe timed out
