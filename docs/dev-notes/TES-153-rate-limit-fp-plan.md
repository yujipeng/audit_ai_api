# TES-153 Story-5 实现计划（P4 rate-limit-fp）

> Issue: TES-153 / Epic: TES-97 / Story 分支: `story/audit-ai-relay/probe-core/rate-limit-fp`
> 合并目标: `epic/audit-ai-relay/probe-core`
> 工时估计: 0.75 d / TDD 红-绿-重构

## 1. 范围

实现 `api_relay_audit/probe/rate_limit_fp.py` 与对应 dual-distribution Section（标准 `audit.py`），新增测试 `tests/test_probe_rate_limit.py` + `tests/test_dual_distribution_parity.py` 同步 Section 检查。

## 2. 接口契约（PRD §3.4 + design §7 Story-5）

```python
def probe_rate_limit(
    client,                # APIClient（含 raw_request / api_key）
    *,
    enabled: bool = True,  # CLI --no-rate-limit-probe → False
    baseline: int = 12,    # F1 决议：12+4（PM 已在 PRD §3.4 / §5.1 C4 / §7.1 R2 沉淀）
    burst: int = 4,
    min_gap_s: float = 0.25,  # PRD §7.1 R2 burst 间隔 ≥ 250ms
    sleep: Callable[[float], None] = time.sleep,   # 注入便于测试
    now:   Callable[[],      float] = time.monotonic, # 注入便于测试
) -> RateLimitResult
```

- `enabled=False` → 立刻返回 `RateLimitResult(status="ok", probe_disabled=True, compliance="absent", signals=["rate_limit:probe_disabled"])`，**不发任何 HTTP**，verdict 不参与失败。
- 真实流程：
  1. **baseline 阶段**：顺序 12 次 `POST /v1/chat/completions`（或类 OpenAI 接口）`max_tokens=1` 最小 payload；解析每个响应 header 内的 `x-ratelimit-*` / `anthropic-ratelimit-*`；
     - 若某次返回 `429` → 立即停（`triggered_429=True`，记录 `samples_to_429`、`retry_after_pattern`、`envelope_429`），**不重试**；
  2. **burst 阶段**（仅在 baseline 未触 429 时）：4 次紧接发出的 `max_tokens=1` 请求，相邻间隔 ≥ `min_gap_s`（用 `now()` 估时 + `sleep` 补足）；同样监测 429；
- 任一阶段触发 429 后立即停（PRD §6.2 A7：「triggered_429 后无重试」）。

### 输出 (`RateLimitResult`)

| 字段 | 取值 |
| --- | --- |
| `status` | `"ok"`（正常完成） / `"error"`（全部 transport 失败）/ `"degraded"`（fail-fast/降级在 Story-6 处理，本片只用 `"ok"`/`"error"`） |
| `rpm_observed` | 从 `x-ratelimit-limit-requests` 或 `anthropic-ratelimit-requests-limit` 抽取，缺则 None |
| `headers_seen` | 真正出现过的 rate-limit 头部名（排序去重） |
| `envelope_429` | `triggered_429=True` 时根据 429 响应体调用 `detect_envelope`（复用 P2 的分类）；否则 `"absent"` |
| `retry_after_pattern` | 解析 `Retry-After` 头：纯数字 → `"seconds"`；含 `GMT` / `:` → `"http_date"`；缺 → `"unknown"`（仅在 triggered_429 时设置） |
| `burst_window_s` | 第一次到最后一次 burst 间间隔 round(int) 秒，仅在跑完 burst 时设；否则 None |
| `triggered_429` | bool |
| `samples_to_429` | 在 baseline+burst 序列内第几次请求触发了 429（1-based） |
| `compliance` | 三态分类：`"openai"`（headers_seen 出现 `x-ratelimit-*` 中至少一个 OpenAI 头部）/ `"anthropic"`（headers_seen 出现 `anthropic-ratelimit-*`）/ `"absent"`（两族头都没出现） |
| `probe_disabled` | True iff `enabled=False` |
| `signals` | 短字符串列表：`rate_limit:triggered_429`、`rate_limit:probe_disabled`、`rate_limit:compliance_openai`、`rate_limit:compliance_anthropic`、`rate_limit:compliance_absent`、`rate_limit:transport_error` 等 |
| `error` | 仅 transport 全部失败时 `ProbeError(code="transport_error:<idx>", message=redact_error(...))` |

### compliance 分类规则
- 至少出现 `x-ratelimit-limit-requests` / `x-ratelimit-limit-tokens` / `x-ratelimit-remaining-*` / `x-ratelimit-reset-*` 任一 → `openai`
- 至少出现 `anthropic-ratelimit-requests-*` / `anthropic-ratelimit-tokens-*` / `anthropic-ratelimit-input-tokens-*` 任一 → `anthropic`
- 同时出现 OpenAI + Anthropic 头部 → `openai`（OpenAI 优先；签出现实情况里多见于 OpenAI 兼容代理）
- 均不出现 → `absent`

## 3. 请求构造

复用 `client.raw_request(method="POST", path="/v1/chat/completions", headers={"Authorization": f"Bearer {client.api_key}"}, body=...)`。Body 是固定 JSON：

```json
{"model": "gpt-4o-mini", "max_tokens": 1, "messages": [{"role":"user","content":"."}]}
```

模型名常量 `_PROBE_RL_MODEL = "gpt-4o-mini"`（design §7 / PRD §3.1 P4 注释「最小请求」）。Anthropic 兼容代理也会接受这个 path（auth_sniff 已经验证两族关于 `/v1/models` 的兼容性，本切片同样依赖 raw_request）。

## 4. 头部解析

```python
# 大小写不敏感 — http header 字典视实现而定；统一 lower() 后查表
_OPENAI_HEADERS  = {"x-ratelimit-limit-requests", "x-ratelimit-limit-tokens",
                    "x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
                    "x-ratelimit-reset-requests",  "x-ratelimit-reset-tokens"}
_ANTHROPIC_HEADERS = {"anthropic-ratelimit-requests-limit",
                      "anthropic-ratelimit-requests-remaining",
                      "anthropic-ratelimit-requests-reset",
                      "anthropic-ratelimit-tokens-limit",
                      "anthropic-ratelimit-tokens-remaining",
                      "anthropic-ratelimit-tokens-reset",
                      "anthropic-ratelimit-input-tokens-limit",
                      "anthropic-ratelimit-input-tokens-remaining",
                      "anthropic-ratelimit-output-tokens-limit",
                      "anthropic-ratelimit-output-tokens-remaining"}
```

`rpm_observed`：`headers.get("x-ratelimit-limit-requests")` → int，否则 `headers.get("anthropic-ratelimit-requests-limit")` → int，否则 `None`。

`Retry-After`:
- 纯整数（`"30"`） → `"seconds"`
- 包含 `,` 或 `GMT` 或 `:`（HTTP date） → `"http_date"`
- 否则 → `"unknown"`

## 5. dual-distribution Section（audit.py）

插入在现有 `# === /models_diff helpers ===` 之后，`# Section 4: CLI` 之前。

标记：

```python
# === probe rate-limit ===
# ... 同模块函数体（detect_compliance / parse_retry_after / probe_rate_limit）
# === /probe rate-limit ===
```

新增 `tests/test_dual_distribution_parity.py` 两个测试：
- `test_probe_rate_limit_section_present_in_standalone`（标记 + 关键 needle 字符串：`def probe_rate_limit`、`def _rl_classify_compliance`、`"openai"`、`"anthropic"`、`"absent"`、`"rate_limit:triggered_429"`）
- `test_probe_rate_limit_behavior_parity`（同 mock 三场景：openai-headers / anthropic-headers / triggered_429）

## 6. 测试矩阵（TDD 红阶段）

`tests/test_probe_rate_limit.py`：

| 类 | 用例 | 验收对应 |
| --- | --- | --- |
| `TestClassifyCompliance` | openai-headers / anthropic-headers / absent / mixed → openai-first | F4 三合规态 |
| `TestParseRetryAfter` | seconds / http_date / 空 / 含 `:` | retry_after_pattern |
| `TestProbeDisabled` | `enabled=False` → 不调 raw_request + probe_disabled=True + signals 含 `rate_limit:probe_disabled` + compliance="absent" | A7 `--no-rate-limit-probe` skip 行为 |
| `TestMaxRequests` | mock 正常返回 200，验证 raw_request 调用次数 ≤ 16；当一切正常时确切 == 12 + 4 = 16 | A7 max_requests ≤ 16 |
| `TestBurstGap` | 用注入的 `sleep` / `now` 计数：burst 阶段相邻请求的 `now()` 间隔 ≥ 0.25s；验证 `sleep(...)` 被调用且参数 ≥ 0 | R2 ≥250ms |
| `TestTriggered429Stops` | 第 5 个响应 status=429，验证 raw_request 被调用恰好 5 次，triggered_429=True，samples_to_429=5，**无重试** | A7 triggered_429 不重试 |
| `TestEnvelope429Detection` | 429 响应体为 openai-style / anthropic-style / absent | A7 envelope_429 |
| `TestRpmObserved` | 头部含 `x-ratelimit-limit-requests: 60` → rpm=60；anthropic-ratelimit-requests-limit fallback | rpm_observed |
| `TestComplianceClassificationFromResponses` | 仅 openai 头出现 → compliance="openai" + signal；仅 anthropic → "anthropic"；都无 → "absent"；mixed → "openai" | 三合规态 |
| `TestTransportError` | 所有请求 `error="connect: connection refused"` → status="error"，signals 含 `rate_limit:transport_error`，error.code = `transport_error:0` | error 路径 |
| `TestBurstSkippedOn429` | baseline 内触 429，burst 不发生（请求总数 < 16） | triggered_429 后无重试 |

外加 `tests/test_dual_distribution_parity.py` 两个 + Story-1 `tests/test_probe_schema_parity.py` 可能需要的字段联动检查（已存在；P4 字段不会动）。

## 7. 行覆盖率 ≥ 90%

实现保持函数化、分支扁平；分支：enabled/disabled / 429 命中位置（baseline / burst / 全过）/ compliance 三态 / Retry-After 三态 / transport_error。每条分支至少一个测试用例。

## 8. 提交节奏

1. commit-1：`docs/dev-notes/TES-153-rate-limit-fp-plan.md`（本文）
2. commit-2：tests/test_probe_rate_limit.py 全红（新增 ≈ 30 用例）+ parity 两个新测试（红）
3. commit-3：`api_relay_audit/probe/rate_limit_fp.py` 实现 + Story-5 audit.py Section 同步 + parity 验证通过；运行 epic 全套件
4. （若必要）commit-4：重构 + 补充覆盖率

commit message 格式（v5 §8）：`feat(probe): ... [TES-153] [TES-97]`。

## 9. 风险与回退

- F1 软门超时 17:30 UTC：若项目负责人选 (B)/(C)，PJM 会发补丁式指令；本设计已将 `baseline` / `burst` / `enabled` 全部参数化，可以通过常量调整即满足。
- 429 envelope_detection 复用 P2 的 `detect_envelope`；本模块以 fallback 内嵌相同算法保持独立单元可测，但若 import auth_sniff 影响打包，可改为函数内 local import（design §7 unconstrained）。
