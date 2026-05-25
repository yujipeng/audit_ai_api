---
issue: [TES-107](mention://issue/88b1b282-4243-454a-803c-7d3cc035a674)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
subsystem: probe-core
depends_on_prd: [TES-102](mention://issue/1b89ac0d-8043-47c9-99ad-e5ad3d39a910)（PRD v0.1, draft, 落盘 commit 9125761/3ccd2e2）
status: draft
reviewers: 资深架构师 / 项目管理专家 / 资深开发工程师代表
---

# S1 probe-core Design（探测核心子系统）

> 上游 Epic：[TES-97](mention://issue/fe5fb483-8cd4-4cc1-8fe3-45f78ff72696)　顶层父：[TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
> 上游 PRD：[TES-102](mention://issue/1b89ac0d-8043-47c9-99ad-e5ad3d39a910)（v0.1, draft，本 design 据此对齐）
> 调研基线：[TES-94](mention://issue/f165c62b-225f-45bc-843a-0322d799c747)（同类项目 + 测试维度全景 + 我此前对 detectors 9 切片的回应）

**对齐声明**：本 design 在初稿后 rebase 拉取到 origin/feature/audit-ai-relay 时发现 TES-102 PRD 已于同日先于本 design 落盘（commit 9125761/3ccd2e2）。已据 PRD §3-§6 + §10 对齐：probe 命名（reachability/auth_sniff/models_diff/rate_limit_fp）、并行执行 + fail-fast 依赖、CLI flag（`--probe-only` / `--skip-probe` / `--no-rate-limit-probe` / `--models-ref-url` / `--no-fail-fast`）、JSON schema_version 契约、references/official_models_*.json 数据源（JSON 非 YAML）。PRD §7.2 Q1-Q4 在本 design §10 给出架构侧推荐答复。

---

## 1. 上游 PRD 概述

### 1.1 PRD（TES-102 v0.1）摘要

PRD 把 S1 范围确定为 4 个原子探针：

- **P1 reachability**：可达性 + TLS（不消耗 token）
- **P2 auth-sniff**：鉴权形态嗅探（不消耗 token）
- **P3 models-list-diff**：`/v1/models` 清单与官方静态参考清单 diff（不消耗 token）
- **P4 rate-limit-fingerprint**：限速指纹（**默认 12 baseline + 4 burst 极少 token 调用，可关闭**）

聚合产物为 `ProbeReport`（schema_version 1.0），含整体 verdict ∈ {pass / warn / fail} + 4 个分项产物。**默认行为变更**（PRD §3.3）：master 默认无 probe，本切片落地后 `audit.py` 默认带 probe + fail-fast；`--skip-probe` 回到 master 行为。

### 1.2 本 design 切片范围

- **承接 PRD §3-§6**：把 4 个探针 + 1 个聚合器变成可被 PJM 拆 Story、被 dev 实现的代码骨架；
- **细化 PRD §3.1 表所未约束的「探针之间的实现协同」**：依赖图 / fail-fast 顺序 / 并发模型 / budget 全局聚合；
- **冻结对下游切片的接口契约**：让 detectors-{fingerprint, prompt-integrity, response, protocol, advanced} 的 design 切片对「ProbeReport 字段」有单点 review 依据；
- **回答 PRD §7.2 开放问题 Q1-Q4** 的架构推荐（见 §10）。

### 1.3 不在本切片范围

模型指纹算法（归 S2 purity-eval / detectors-fingerprint）、提示词注水（归 detectors-prompt-integrity）、流式时序（归 detectors-response）、真源 twin call（归 audit-engine 后续 design 切片）。**probe-core 仅产清单级 / 头部级 / 状态码级数据，不做语义判定**。

### 1.3 与现有代码的关系（已验证）

读取 feature/audit-ai-relay HEAD（6693aca）后实测：

| 现有模块 | 与 probe-core 的关系 | 复用形态 |
|---|---|---|
| `api_relay_audit/client.py:APIClient` | **强复用**：base_url/api_key/model/timeout 协议适配层 | probe 全程仅通过 APIClient 出网，不绕过 |
| `api_relay_audit/transparent_log.py:TransparentLogger` | **强复用**：所有 probe 请求经 APIClient 自动入 JSONL hash-only 日志 | 无需修改 |
| `api_relay_audit/reporter.py:Reporter` | **复用**：probe-core 输出一个 `## Step 0 — Endpoint Probe` 段，沿用 flag(level, msg) API | 无需修改 |
| `api_relay_audit/infra_fingerprint.py`（Step 12） | **形态参考**：3 个 GET probe + verdict dict，**probe-core 是它的标准化扩张**（4 能力、统一 schema） | 不合并，但参考其失败 fail-soft 风格 |
| `api_relay_audit/latency_variance.py`（Step 13） | **协同**：probe-core 的 reachability 与 rate_limit_fp 探测产物可能影响 Step 13 解读，但不交叉调用 | 解耦 |
| `audit.py`（root standalone）| **dual-distribution invariant**：probe-core 必须以 `Section` block 形式同步 inlined 到 root | 强约束，见 §5 / §6 |
| 既有 detectors（`identity_patterns / tool_substitution / error_leakage / stream_integrity / web3` 等） | **解耦**：probe-core 不消费 detector 产物，detectors 也不消费 probe 的中间产物——仅消费最终 ProbeReport | 单向上游 |

---

## 2. 候选方案（brainstorming 9 步产物）

设计探索按 brainstorming 9 步推进：①意图明确 ②约束识别 ③多候选生成 ④挑战 ⑤POC 引用 ⑥取舍矩阵 ⑦推荐 ⑧回退 ⑨交付。下面 §2.1-2.4 是 ③-⑤，§3 是 ⑥-⑨。

### 2.1 候选 A：扁平模块函数集（"flat probe set"）

每个能力一个独立模块函数，调用方各自 import：

```
api_relay_audit/probe/
  reachability.py    check_reachable(client) -> dict
  auth_sniff.py      probe_auth(client) -> dict
  models_diff.py     fetch_models(client) -> dict
  rate_limit_fp.py   probe_rate_limit(client) -> dict
```

**与谁共生**：直接复刻 `infra_fingerprint.py` 与 `latency_variance.py` 的风格——模块顶层函数 + 返回 dict + 由 `scripts/audit.py:test_*()` 串起来。

**优点**：
- 实现成本最低（约 +500 LOC 模块版 + +500 LOC 同步进 audit.py root）；
- 与现有 13 步审计的代码风格完全一致，新人零理解负担；
- dual-distribution 同步路径最直接。

**缺点**：
- 4 个 probe 之间无共享调度（如 P1 失败时是否短路 P2、P3 是否在 P1 通过后才执行——PRD §3.1 要求的 fail-fast 拓扑无统一实现位），下游 detector 如果要"按 probe 结果决策"，需要自己拼接各 dict；
- 4 个 dict 的 schema 散落在 4 个文件，下游 S2/S3/S4 在 design 阶段无法对"我能拿到什么"做单点 review；
- 复用维度低：S2 perf-bench 已经在 `perf/streaming.py:fetch_models()` 重新实现了一次 `/v1/models` 拉取（实测确认），扁平方案会延续这种重复。

### 2.2 候选 B：Probe ABC + ProbeRunner 编排（"OO orchestrator"）

抽象基类 + 依赖图调度：

```
api_relay_audit/probe/
  base.py            class Probe(ABC): name, dependencies, run(ctx) -> ProbeResult
  runner.py          class ProbeRunner: topological_sort + 并发池 + 退避策略 + 结果聚合
  reachability.py    class ReachabilityProbe(Probe)
  auth_sniff.py      class AuthSniffProbe(Probe)
  models_diff.py     class ModelsDiffProbe(Probe)
  rate_limit_fp.py   class RateLimitProbe(Probe)
```

**优点**：
- 扩展性最强：新增 probe 只需注册一个 `Probe` 子类，runner 自动处理依赖与并发；
- 统一退避/重试/budget 控制，避免 4 个模块各写一遍；
- 测试友好——可对单个 Probe 子类做单元测试，对 runner 做集成测试。

**缺点 / 致命问题**：
- **严重违反 dual-distribution invariant**：把 ABC 层级、ProbeRunner 调度逻辑、4 个具体子类全部 inline 进 `audit.py` root（当前 ~2500 LOC），估计 +800-1000 LOC，单文件接近 3500 LOC 与"一次性 curl 下载即用"的产品形态冲突。CLAUDE.md "Contribution Philosophy" 明确 KISS：「Do not add features because they might be useful — add them when a real user need has been reported」；
- **YAGNI 违反**：当前 4 个 probe 没有非线性依赖图、没有并发需求（探测预算太小，串行 ≤ 15 秒已够），抽象层在 MVP 阶段无用武之地；
- 实现成本翻倍（约 +1200 LOC），首批 Story 数量从 6 涨到 8-9；
- 与现有 13 步审计的"模块函数"风格断层，未来 detectors 仿照这套抽象会导致全栈重构压力。

### 2.3 候选 C（推荐）：模块函数 + dataclass + 轻量聚合器（贴合 PRD §3.1 / §4.1）

每个能力一个模块函数，统一返回 dataclass 形式的 ProbeResult；`aggregator.py` 是一个 ~80 LOC 的聚合器（并行 4 探针 + fail-fast 依赖 + verdict 推断）：

```
api_relay_audit/probe/
  __init__.py       run(base_url, key, **opts) -> ProbeReport         (PRD §3.1 入口)
  types.py          dataclass ProbeReport / ReachabilityResult / ...
  reachability.py   check_reachability(client) -> ReachabilityResult
  auth_sniff.py     probe_auth_sniff(client) -> AuthSniffResult
  models_diff.py    fetch_models_diff(client, vendor_hint) -> ModelsDiffResult
  rate_limit_fp.py  probe_rate_limit(client, enabled=True) -> RateLimitResult
  aggregator.py     run_probes(...) + compute_verdict(report) -> str
  schema_check.py   validate(report_json) -> None                     (PRD §6.2 A3 验收)
  references/
    official_models_{openai,anthropic,gemini}.json                    (PRD §4.1 数据源)
    schema.json                                                       (JSON Schema 定义)
```

**优点**：
- 实现成本接近候选 A（约 +650 LOC 模块版，其中 dataclass + 聚合器 overhead ~80 LOC）；
- **统一 schema 是 S1 给下游的唯一稳定契约**——detectors-{response, fingerprint, protocol, advanced} 的 design 切片可以围绕 ProbeReport 字段 review，单点对齐；
- 与现有 `infra_fingerprint.py` (239 LOC) / `latency_variance.py` (237 LOC) 形态对齐，dual-distribution 同步成本可控（单 Section block，~700 LOC inlined）；
- **PRD §3.1 / §4.1 显式给出的就是这个形态**，无认知偏差；
- 后续要演进到候选 B 的并发/依赖图时，可在 `aggregator.run_probes()` 内部重构，**对外契约不变**。

**缺点 / 接受的限制**：
- ProbeReport 字段需要在 S2/S3/S4 设计时回头加新字段（接受——schema 的"加字段不破坏"是 dataclass 的天然保证）。

### 2.4 POC 引用与回退方案

| POC 证据 | 用途 |
|---|---|
| 现有 `api_relay_audit/infra_fingerprint.py`（239 LOC）+ `api_relay_audit/latency_variance.py`（237 LOC）实测在 audit.py Step 12/13 稳定跑通 | **正例**：候选 C 的目标稳态——模块函数 + 简单数据返回，已被 13 步审计验证可行 |
| 现有 `api_relay_audit/perf/runner.py`（297 LOC，简单顺序 runner，不抽 ABC） | **反例**：perf 子模块已经做过一次"要不要抽 OO"的选择，选择了不抽（顺序执行 + dataclass 结果）——候选 C 沿用同一选择 |
| `tests/test_dual_distribution_parity.py`（已存在）+ CLAUDE.md "Dual Distribution Model" 章节 | **强约束证据**：候选 B 与该约束冲突，候选 A/C 兼容 |
| `api_relay_audit/perf/streaming.py:fetch_models()` 重复实现了一次 `/v1/models` 拉取 | **复用证据**：候选 A 会延续这种重复，候选 C 通过 ProbeReport 暴露一份"打一次给所有 detector 用"的清单 |

**回退方案**：如果候选 C 在 Story 拆解阶段被发现 ProbeReport schema 与 S2-S4 真实需求严重错位，回退到候选 A（每个 probe 直接返回 dict），不阻塞 Epic 关键路径；ProbeReport 的迁移成本仅是 4 个模块的 `return` 类型从 dict 改回 dataclass 或反之。

---

## 3. 取舍与推荐

### 3.1 评估矩阵

| 维度 | A: flat probe set | B: OO orchestrator | C: dataclass + 轻量协调器（**推荐**） |
|---|---|---|---|
| 实现成本（首发 LOC） | +500 模块 +500 root | +1200 模块 +1000 root | +650 模块 +700 root |
| 扩展性（新 probe 加入） | 中（需调用方手动接入） | 强（注册即用） | 中（与 A 同，但接口契约稳） |
| dual-distribution 同步 | ✅ 顺畅 | ❌ 接近单文件 LOC 上限 | ✅ 顺畅（单 Section block） |
| 与现有代码风格一致 | ✅✅ | ❌ 风格断层 | ✅✅（与 infra_fingerprint / latency_variance 同形） |
| 下游 S2-S4 复用 | 弱（4 个独立 dict） | 优 | 优（单 ProbeReport 契约） |
| KISS / YAGNI | ✅ | ❌ | ✅ |
| schema 单点 review | ❌（散落 4 文件） | ✅ | ✅ |
| MVP 速度 | 最快 | 最慢 | 仅比 A 慢 ~10% |
| 演进到 B 的路径 | 需重构调用方 | — | 在 `run_probes()` 内部演化，**对外契约稳定** |

### 3.2 推荐：候选 C

**为什么选 C 而不是 A**：

下游 S2-S4 都会消费 `/v1/models` 清单（perf 用它做并发模型选择、fingerprint 用它做声明 vs 实测对照、protocol 用它做错误码探测目标）。候选 A 让每个 detector 自己打 /v1/models，会出现：
- 重复打同一接口（已经在 `perf/streaming.py:fetch_models()` 与 `scripts/audit.py:test_models()` 重复了一次），增加被中转站标记/限流风险；
- 每个 detector 各自做 vendor_breakdown 解析，逻辑分散；
- 4 个 dict 的 schema 无单点 review，下游 design 切片很难提前对齐。

**为什么不选 B**：

CLAUDE.md "Contribution Philosophy" 第一原则是「User-feedback-driven, not speculative」；ROADMAP §2.6 列出永久 out-of-scope 的项目都因"speculative complexity"被拒。候选 B 的依赖图/并发能力**已被 PRD §3.1 给出的小型 fail-fast 拓扑（P1→{P3}、P2→{P4}）所替代**——这是 4 节点的简单 join，不需要 ABC + Runner 这套架构。

进一步：candidate B 的核心价值（统一退避策略 + budget 控制）可以以 ~30 LOC 的形式纳入候选 C 的 `aggregator.run_probes()`（每个 ProbeResult 持一个 `probe_calls` 字段，聚合器全局累加到 `ProbeReport.total_http_calls`），不需要 ABC + Runner 这套架构。

### 3.3 聚合器并发与 fail-fast 拓扑（落实 PRD §3.1 / §3.2）

PRD 显式要求 4 探针中 P3 / P4 仅在 P1 / P2 通过后并行执行；候选 C 的 `aggregator.run_probes()` 因此**不是纯串行**，而是两阶段并行：

```
                   ┌── P1 reachability ──┐
                   │                     ├─ if both pass → ┌── P3 models_diff ──┐
input(base_url,    │                     │                 │                    │
key, vendor_hint)──┤                     │                 │                    ├─ aggregate ─→ ProbeReport
                   │                     │                 │                    │
                   └── P2 auth_sniff ────┘                 └── P4 rate_limit ───┘
                       (P1, P2 并发)                          (P3, P4 并发)
```

实现以 `concurrent.futures.ThreadPoolExecutor(max_workers=2)` 完成两阶段并行（参考 `perf/runner.py` 已有的 ThreadPoolExecutor 模式，避免 asyncio 引入）。

`--no-fail-fast` 模式：P3 / P4 即使 P1 / P2 失败也照打，但 ProbeResult.status 标 `degraded`，verdict 由聚合器规则推断（见 §4.3）。

### 3.4 与 detector 9 切片的呼应

我在 TES-94 提出过 detector 拆 9 切片（`audit-engine / provider-adapter / config-and-secrets / detectors-fingerprint / detectors-prompt-integrity / detectors-response / detectors-protocol / detectors-advanced / report-generator / cli-and-ui`）。本 design 落到那张拓扑里：

- probe-core 是 **provider-adapter** 之上、**audit-engine** 之内的一个独立模块；
- probe-core **不是** detector——它产 ProbeReport，由 audit-engine 在调度 detector 前喂给后者；
- audit-engine 的 twin call orchestrator 是 detectors-fingerprint 的前置；probe-core 是 audit-engine 的前置。**层级顺序：provider-adapter → probe-core → audit-engine → detectors-***。

---

## 4. 接口契约 / 数据模型

PRD §3.5 已给出 JSON 顶层 schema 草案（schema_version 1.0、verdict、4 个 probe 分项产物 + infra_hint 派生字段）。本节把它落到 dataclass + 字段级 schema + 给下游 detector 的消费契约。

### 4.1 ProbeResult / ProbeReport dataclass

```python
# api_relay_audit/probe/types.py

from dataclasses import dataclass, field
from typing import Optional

# 单个探针的状态（独立于聚合 verdict）
PROBE_STATUS = ("ok", "degraded", "error")  # PRD §4.2 强约束：禁止 raise 吞错误

# 聚合 verdict（PRD §3.1 / §6.2 / §10）
PROBE_VERDICT = ("pass", "warn", "fail")    # fail → audit.py exit code 2 短路 13 步

@dataclass
class ProbeError:
    """PRD §4.2 要求：所有探针错误必含 code/message，不允许吞掉。"""
    code: str       # e.g. "tcp_refused" / "tls_handshake_failed" / "401_returned"
    message: str    # 经 transparent_log.redact_error 处理后的安全字符串

@dataclass
class ReachabilityResult:
    """P1 产物。"""
    status: str                              # PROBE_STATUS
    tcp_ok: bool
    tls_ok: bool
    tls_chain_summary: Optional[str] = None  # e.g. "Let's Encrypt / RSA-2048 / 90d"
    dns_resolves: bool = True
    http_status_root: Optional[int] = None   # base GET（非 /v1）状态码
    latency_ms: Optional[int] = None
    fallback_to_curl: bool = False           # 是否触发 client._handle_ssl_error 路径
    signals: list[str] = field(default_factory=list)
    error: Optional[ProbeError] = None

@dataclass
class AuthSniffResult:
    """P2 产物。"""
    status: str
    accepted_schemes: list[str] = field(default_factory=list)  # e.g. ["bearer"] / ["x-api-key"] / ["bearer", "x-api-key"]
    envelope_401: str = "unknown"            # "openai-style" / "anthropic-style" / "non-standard" / "absent"
    envelope_403: str = "unknown"
    key_position: str = "unknown"            # "Authorization" / "x-api-key" / "<custom-header>"
    classification: str = "unknown"          # "strict" / "permissive" / "broken"（permissive 是严重风险）
    signals: list[str] = field(default_factory=list)
    error: Optional[ProbeError] = None

@dataclass
class ModelsDiffResult:
    """P3 产物。"""
    status: str
    declared: list[str] = field(default_factory=list)       # 中转站 /v1/models 返回的 id 集合
    declared_count: int = 0
    official_reference: list[str] = field(default_factory=list)  # 来自 references/official_models_<vendor>.json
    catalog_version: Optional[str] = None                   # 静态参考清单版本号（PRD R1 R1 缓解）
    extra_in_relay: list[str] = field(default_factory=list) # 中转有 / 官方无（含 suspicious_aliases 子集）
    missing_in_relay: list[str] = field(default_factory=list)  # 官方有 / 中转无（用户期待模型缺失）
    suspicious_aliases: list[str] = field(default_factory=list)  # 启发式可疑命名（"gpt-5-pro" / "claude-3-mini" 等）
    claimed_model_match: str = "unknown"     # "exact" / "alias" / "missing"（仅当 audit run 指定 --model）
    vendor_breakdown: dict[str, int] = field(default_factory=dict)  # id 前缀分组
    signals: list[str] = field(default_factory=list)
    error: Optional[ProbeError] = None

@dataclass
class RateLimitResult:
    """P4 产物。"""
    status: str
    rpm_observed: Optional[int] = None       # 从 RateLimit-Limit 头解析的速率上限
    headers_seen: list[str] = field(default_factory=list)   # 实际见到的限速相关头名
    envelope_429: str = "absent"             # "openai-style" / "anthropic-style" / "non-standard" / "absent"
    retry_after_pattern: str = "unknown"     # "fixed-seconds" / "http-date" / "exponential" / "absent"
    burst_window_s: Optional[int] = None
    triggered_429: bool = False
    samples_to_429: Optional[int] = None
    compliance: str = "unknown"              # "openai-compatible" / "anthropic-compatible" / "non-standard" / "absent"
    probe_disabled: bool = False             # True 当 --no-rate-limit-probe
    signals: list[str] = field(default_factory=list)
    error: Optional[ProbeError] = None

@dataclass
class InfraHint:
    """复用 infra_fingerprint.classify_framework() 的派生字段（PRD §3.5）。"""
    framework: str                           # "new-api" / "one-api" / "lobechat-relay" / ...
    confidence: str                          # "confirmed" / "tentative" / "unknown"

@dataclass
class ProbeReport:
    """PRD §3.5 顶层 schema 的 dataclass 化。schema_version='1.0' 冻结。"""
    schema_version: str                      # "1.0"
    generated_at: str                        # RFC3339 UTC
    input_base_url: str
    input_key_fingerprint: str               # sha256 前 8 byte（PRD §4.2 / §5.1 C2 强约束）
    input_vendor_hint: str                   # "auto" / "openai" / "anthropic" / "gemini"
    verdict: str                             # PROBE_VERDICT
    reachability: ReachabilityResult
    auth_sniff: AuthSniffResult
    models_diff: ModelsDiffResult
    rate_limit: RateLimitResult
    infra_hint: Optional[InfraHint] = None   # 派生字段，可缺
    total_http_calls: int = 0                # 全 probe 合计 HTTP 请求数（成本核算 / R2 兜底）

    def has_fatal(self) -> bool:
        """verdict='fail' 时为 True；audit.py 据此短路（PRD §3.3）。"""
        return self.verdict == "fail"

    def to_dict(self) -> dict:
        """PRD §3.5 JSON schema 序列化（schema_version=1.0 字段顺序冻结）。"""
        ...
```

### 4.2 关键 schema 决策与 PRD 对应

| 决策 | 来源 | 备注 |
|---|---|---|
| `schema_version="1.0"` 顶层字段固定 | PRD §3.5 / §6.2 A3 | 字段加允许（向前兼容），删/改需 v5 design 回炉 |
| `key_fingerprint` 仅存 sha256 前 8 byte | PRD §4.2 / §5.1 C2 / §6.2 A8 | dataclass 不接受 raw key 入构造，构造期截断 |
| 4 个 ProbeResult 强制含 `status ∈ {ok, degraded, error}` + `error.code` | PRD §4.2 | 禁止吞错误；`tests/test_probe_schema_parity.py` 静态断言每个 dataclass 字段 |
| `verdict` 三态而非二态 | PRD §3.3 / §10 | warn 路径让 audit.py 进入 13 步但报告头插警告条 |
| `infra_hint` 是派生字段，可缺 | PRD §3.5 末段 | 由 `aggregator.py` 调用 `infra_fingerprint.classify_framework()` 后嵌入 |
| `catalog_version` 写入 ModelsDiffResult | PRD §4.3 / R1 | references/*.json 自带版本号，CI 60 天过期告警（PRD R1） |

### 4.3 verdict 推断规则（聚合器核心）

PRD §3.3 给出 verdict 与 audit.py 短路行为，但未明确 verdict 聚合规则。本 design 冻结如下：

```
verdict = "fail" 当且仅当：
    reachability.status == "error"
    OR auth_sniff.classification == "broken"
    OR auth_sniff.classification == "permissive"   # 中转站无鉴权是严重风险
    OR models_diff.status == "error" AND audit run 指定了 --model

verdict = "warn" 当 verdict ≠ "fail" 且满足任一：
    reachability.fallback_to_curl == True           # TLS 异常但勉强通
    models_diff.suspicious_aliases != []            # 可疑命名（弱信号）
    models_diff.claimed_model_match == "alias"      # 用户 --model 匹配到 alias 而非 exact
    rate_limit.compliance == "absent"               # 无限速可观测性（合规风险）
    任一 ProbeResult.status == "degraded"

verdict = "pass" 当所有 ProbeResult.status == "ok" 且无上述 warn 条件
```

聚合器实现见 `api_relay_audit/probe/aggregator.py:compute_verdict(report) -> str`，单元测试断言 12 个组合（4 status × 3 主观规则 + 3 corner case）。

### 4.4 模块结构（与 PRD §4.1 对齐）

PRD §4.1 已给出模块结构骨架，本 design 补 dataclass 文件 + tests 文件清单：

```
api_relay_audit/
  probe/
    __init__.py                       # 暴露 run(base_url, key, **opts) -> ProbeReport（PRD §3.1 入口）
    types.py                          # ★本 design §4.1 新增；ProbeResult / ProbeReport / ProbeError dataclass
    reachability.py                   # P1 实现
    auth_sniff.py                     # P2 实现
    models_diff.py                    # P3 实现
    rate_limit_fp.py                  # P4 实现
    aggregator.py                     # ProbeReport 组装 + compute_verdict
    schema_check.py                   # ★本 design 新增；JSON Schema 校验（PRD §6.2 A3 验收命令）
    references/
      official_models_openai.json     # PRD §4.1
      official_models_anthropic.json
      official_models_gemini.json
      schema.json                     # ★本 design 新增；JSON Schema 定义文件
tests/
  test_probe_reachability.py          # PRD §6.2 A4
  test_probe_auth_sniff.py            # PRD §6.2 A5
  test_probe_models_diff.py           # PRD §6.2 A6
  test_probe_rate_limit.py            # PRD §6.2 A7
  test_probe_no_key_leak.py           # PRD §6.2 A8
  test_probe_schema_parity.py         # ★本 design 新增；dataclass schema 字符级断言
  test_probe_aggregator.py            # ★本 design 新增；verdict 推断 12 组合
  test_probe_integration.py           # ★本 design 新增；端到端 mock + audit.py Step 0 集成
```

★ 为本 design 在 PRD §4.1 基础上追加的文件，已在 §7 Story 拆分中分配工时。

### 4.5 与下游切片的接口约定

下游切片**只通过 ProbeReport 消费 probe-core 产物**。不允许 import `api_relay_audit/probe/*` 内部模块直接调用 probe 函数。

| 下游切片 | 消费的 ProbeReport 字段 | 用途 |
|---|---|---|
| audit-engine | `has_fatal()` / `total_http_calls` / 全部 `signals` | 决定是否短路 detectors / 全局 budget 核算 / signal-driven detector 路由 |
| S2 detectors-fingerprint（purity-eval） | `models_diff.declared` / `claimed_model_match` / `suspicious_aliases` | twin-call 选用的 truth 模型清单 / 声明-实际 diff 的基线 |
| S2-S4 detectors-prompt-integrity | `auth_sniff.envelope_401` / `key_position` | 决定如何构造注水探测请求的 header / system field 形态 |
| S3 perf-bench-v2（detectors-response） | `reachability.fallback_to_curl` / `signals` / `rate_limit.rpm_observed` | 时序分析时排除 curl 路径噪声 / 并发上限安全阈值（PRD §2.2 场景 C） |
| S4 pricing-compliance（detectors-protocol） | `auth_sniff.accepted_schemes` / `models_diff.declared` | 协议透传探测选用的请求形态 / 计费维度归集 |
| S5 orchestration-report（detectors-advanced） | `rate_limit.compliance` / `infra_hint` | 并发压测前的限流预案 / 框架风险关联 |
| report-generator | 全部 | 报告头部 "Step 0 — Endpoint Probe" 摘要段 |

**字段冻结约定**：本 design 进入 `status: approved` 后，ProbeReport 顶层字段 + 5 个 dataclass schema 进入 **frozen**。新增字段允许（dataclass 兼容），删除/重命名字段需走 v5 design 回炉。CI 通过 `tests/test_probe_schema_parity.py` 做断言。

---

## 5. 性能与容量

### 5.1 HTTP 预算（PRD §3.1 / §5.2 N1-N2 / §6.2 A11-A12）

| Probe | 请求次数 | 单次预算 | 备注 |
|---|---|---|---|
| P1 reachability | 1 GET base + （可选 1 GET /v1） | 8 s（PRD N1） | TLS 握手算入；fallback_to_curl 走 client._handle_ssl_error |
| P2 auth_sniff | 3（valid/invalid/missing） | 8 s | 全是最小请求，期望 401/403 不消耗 token |
| P3 models_diff | 1 GET /v1/models | 8 s（继承 PRD N1） | 仅当 P1 通过 |
| P4 rate_limit_fp | **12 baseline + 4 burst（PRD §5.1 C4 上限）** | 250 ms 间隔（PRD R2） | 仅当 P2 通过；触发 429 即停；可关闭 |
| **合计** | **≤ 22 次** | — | **PRD §5.2 N1：8 秒并行总预算；并行后实测均值 ~6 秒** |

PRD §5.2 N2 要求总流量 < 50 KB（不计 TLS handshake）：probe 全部为 GET 或最小请求，body ≤ 50 byte/次，22 次 ≈ 1.1 KB 出向 + ≤ 50 KB 入向（/v1/models JSON 是最大头，多数 < 30 KB）。

### 5.2 成本

- **token 成本**：仅 P4 的 12 baseline + 4 burst，按 max_tokens=1（≤ 0.0005 美元/次 audit，按 GPT-4o 计价）；
- **monetary cost**：P1/P2/P3 均不消耗 token（GET 或鉴权失败）；
- **rate limit budget**：单 audit ≈ 16 次实际计费请求，**符合 PRD §2.2 场景 A "免 token 消耗 dry-run"** 的近似目标（P4 关闭后完全免 token）。

### 5.3 内存

- ProbeReport 序列化体积 ≤ 50 KB（dataclass 全是 scalar / 短字符串 / 4 厂商模型清单）；
- 不缓存任何响应体（hash-only 入 transparent_log）；
- references/official_models_*.json 启动时一次性 load 到 module-level 常量，~30 KB。

### 5.4 与现有 13 步审计的时长对比

| 阶段 | 时长（典型） |
|---|---|
| **probe-core (Step 0, 新增)** | **~6-8 s（并行）** |
| Step 1-13 已有审计 | ~3-5 分钟 |

probe-core 占用 < 5% 增量，可接受。PRD §6.2 A11 要求 `audit.py --probe-only` 端到端 ≤ 10s（含 Python 启动 ≈ 2s）。

---

## 6. 风险与缓解

| ID | 风险 | 严重度 | 缓解 |
|---|---|---|---|
| R1 | rate_limit_fp 故意触发 429，可能被中转站标记/拉黑测试 key | 中 | PRD §5.1 C4 上限 12+4；提供 `--no-rate-limit-probe`；`max_tokens=1` 最小化成本；触发 429 即停；burst 间隔 ≥250ms（PRD R2） |
| R2 | dual-distribution 同步成本（audit.py 需要 inline ~700 LOC） | 中 | 候选 C 是单模块单 Section block；CI 有 `test_dual_distribution_parity.py` 兜底；Story 拆分明确每个 Story "dual-dist：同步加 `# === probe.X ===` Section" |
| R3 | /v1/models 拉取被中转站缓存欺骗（声明 GPT-5 但实测调不通） | 高 | probe-core 只产 `claimed_model_match: str` 这一弱信号；**真实可用性下沉到 detectors-fingerprint，依赖 audit-engine twin call** —— probe-core 不越界承担 |
| R4 | reachability 探测受 Cloudflare/CDN 影响误报 TLS 状态 | 低 | signals 标 `reachability:cloudflare` 而非 `reachability:tls-suspicious`；让下游决定语义 |
| R5 | auth_sniff 在严格中转站留风控日志 | 低 | 全部 probe 经 transparent_log.py 留 hash-only 痕迹；与现有 13 步审计同等隐私级别；CLAUDE.md "用户隐私" 章节兜底 |
| R6 | 下游 detector 与 ProbeReport 字段约定漂移 | 中 | dataclass + `test_probe_schema_parity.py` schema 字段断言 + `schema_check.py` JSON Schema 断言；冻结契约由 audit-engine design 切片协同 review |
| R7 | references/official_models_*.json 维护成本（厂商发布节奏 ~月度） | 中 | MVP 4 厂商 × top-20 模型 ~80 条，季度人工 review；CI 加 `catalog_version` 过期 60 天 → warning（PRD R1）；长期由 audit-engine twin call 自动校对 |
| R8 | 聚合器 `run_probes()` 内部异常未捕获导致整个 ProbeReport 失败 | 低 | 每个 probe 独立 try/except 包装为 fail-soft ProbeResult；run_probes 即使一个 probe 抛也保证返回 ProbeReport；ProbeError 字段承载 |
| R9 | curl fallback 路径下 reachability 部分字段（dns_resolves）不可观测 | 低 | 字段 typed Optional，置 None 即可；signals 加 `reachability:via-curl` 让下游知情 |
| R10 | 与已有 Step 12 infra_fingerprint 信号重叠（server header 等） | 低 | probe-core 是上游基础探测，Step 12 是 framework 推断；本 design 选择**复用** infra_fingerprint.classify_framework() 作为 ProbeReport.infra_hint 派生字段（PRD §3.5），由 reporter 渲染时去重 |
| R11 | base_url 归一化错误（带或不带 `/v1`，trailing slash） | 中 | 与 PRD R4 同；`api_relay_audit/probe/__init__.py:run()` 入口统一归一化 8 种变体；单测 `test_probe_url_normalization.py`（合并入 Story-1） |
| R12 | P2 auth_sniff 把 key 发到错误 endpoint | 高 | PRD C3 强约束 + Story-3 单测扫 P2 实现里所有 httpx 调用的 host 都来自归一化后的 base_url（与 PRD R6 同） |

---

## 7. 实现拆分建议（给 PJM 拆开发子 issue 用）

**6 个 Story**（建议命名 `Story: S1-probe-core-XXX`），总工时估算 ~3.5-4.5 dev 日，关键路径 ~2 日（Story 1 → 6 串行，2-5 之间可并行）。Story 验收对齐 PRD §6.2 A1-A13。

### Story-1: probe.types ProbeReport / ProbeResult dataclass + JSON Schema 静态文件

**范围**：
- `api_relay_audit/probe/__init__.py` 占位 + 导出 `run`
- `api_relay_audit/probe/types.py` ProbeError + 4 ProbeResult dataclass + ProbeReport + InfraHint
- `api_relay_audit/probe/references/schema.json` JSON Schema 定义文件（PRD §6.2 A3）
- `api_relay_audit/probe/schema_check.py` JSON Schema 校验器
- `tests/test_probe_schema_parity.py` dataclass 字段 + 类型 + schema_version 字符级断言（黄金路径 fixture）
- dual-dist：`audit.py` 同步加 `# === probe types ===` Section block

**工时**：0.5 日　**依赖**：无　**验收**：pytest pass + ProbeReport.to_dict() schema_version=="1.0" + schema_check.py 通过 PRD §3.5 草案 JSON

### Story-2: probe.reachability 可达性 + TLS/DNS/headers（P1）

**范围**：
- `api_relay_audit/probe/reachability.py:check_reachability(client) -> ReachabilityResult`
- 复用 `client._handle_ssl_error` 决定 fallback；不绕过 APIClient
- `tests/test_probe_reachability.py` 含 httpx mock + curl fallback 双路径，覆盖 PRD §6.2 A4 六分支（TCP fail / TLS fail / DNS fail / 200 / 4xx / 5xx）
- dual-dist：audit.py 同步 Section

**工时**：0.5 日　**依赖**：Story-1　**验收**：6 路径覆盖；signals 正确标注

### Story-3: probe.auth_sniff 鉴权嗅探 + 错误码分类（P2）

**范围**：
- `api_relay_audit/probe/auth_sniff.py:probe_auth_sniff(client) -> AuthSniffResult`
- 三探（valid/invalid/missing）+ envelope 解析 + classification 推断（strict/permissive/broken）
- `tests/test_probe_auth_sniff.py` 覆盖 PRD §6.2 A5 五分支（bearer / x-api-key / 自定义 header / 401 / 403）
- `tests/test_probe_no_key_leak.py`（PRD §6.2 A8）：扫 ProbeReport JSON + stdout 中 `sk-` / `Bearer ` / raw key 0 命中
- dual-dist：audit.py 同步 Section

**工时**：0.75 日（含 no-key-leak 单测）　**依赖**：Story-1　**验收**：classification ∈ {strict/permissive/broken}；permissive 必有 red flag；A8 通过

### Story-4: probe.models_diff 清单拉取 + 离线参考 diff（P3）

**范围**：
- `api_relay_audit/probe/models_diff.py:fetch_models_diff(client, vendor_hint) -> ModelsDiffResult`
- `api_relay_audit/probe/references/official_models_{openai,anthropic,gemini}.json`（PRD §4.3，含 catalog_version 元数据）
- 复用 `client.get_models()`；suspicious_aliases 启发式（regex + 命名黑名单）
- `tests/test_probe_models_diff.py` 覆盖 PRD §6.2 A6 三类 diff（extra_in_relay / missing_in_relay / suspicious_alias）+ catalog 格式校验
- `--models-ref-url <url>` 外部覆盖（PRD §10 表）
- dual-dist：audit.py 同步 Section + JSON 内联为 dict 常量

**工时**：1.0 日（含人工策展 80 模型 ID）　**依赖**：Story-1　**验收**：3 类 diff 正确；至少 1 个 suspicious_alias 正例；catalog_version 写入 ProbeReport

### Story-5: probe.rate_limit_fp 限速指纹 + 退避采样（P4）

**范围**：
- `api_relay_audit/probe/rate_limit_fp.py:probe_rate_limit(client, enabled=True) -> RateLimitResult`
- 12 baseline + 4 burst max_tokens=1 顺序请求（**PRD §5.1 C4 硬上限**）+ RateLimit-* 头部解析 + 触发 429 即停 + Retry-After 解析 + ≥250ms 间隔（PRD R2）
- `tests/test_probe_rate_limit.py` 覆盖 PRD §6.2 A7（max_requests ≤ 16 / `--no-rate-limit-probe` 跳过且 verdict 不 fail）+ 三种合规度（openai/anthropic/absent）
- dual-dist：audit.py 同步 Section

**工时**：0.75 日　**依赖**：Story-1　**验收**：compliance 分类正确；triggered_429 触发后无重试；总请求数 ≤ 16

### Story-6: probe.aggregator + audit.py Step-0 集成 + CLI 兼容性

**范围**：
- `api_relay_audit/probe/aggregator.py:run_probes(client, **opts) -> ProbeReport`：两阶段并行（concurrent.futures）+ fail-fast 拓扑（§3.3）+ verdict 推断（§4.3）+ infra_hint 嵌入（PRD §3.5 派生字段）
- `api_relay_audit/probe/__init__.py:run(base_url, key, **opts) -> ProbeReport`：PRD §3.1 入口 + key_fingerprint 截断 + APIClient 构造
- `scripts/audit.py` 新增 Step-0 调用 + reporter 渲染 `## Step 0 — Endpoint Probe` 段
- CLI flag（PRD §10 表）：`--probe-only` / `--skip-probe` / `--no-rate-limit-probe` / `--models-ref-url <url>` / `--no-fail-fast`
- 退出码：pass=0 / warn=1（仅 --probe-only 模式下） / fail=2（PRD §6.2 A2 / A9）
- `tests/test_probe_integration.py` 端到端 mock；A9 fail-fast 短路断言；A13 `--skip-probe` 与 master@6693aca 行为完全一致（diff stdout）
- dual-dist：audit.py 同步 + reporter 渲染 + Step 计数同步

**工时**：1.0 日（含 CLI / dual-dist / verdict 12 组合单测）　**依赖**：Story-1-5　**验收**：完整 audit run 含 Step-0 + ProbeReport 入 transparent_log JSONL + reporter Step-0 段渲染正确 + PRD §6.2 A1/A2/A9/A11/A12/A13 全过

### Story 依赖 DAG

```
Story-1 (types) ───┬── Story-2 (reachability)  ──┐
                   ├── Story-3 (auth_sniff)    ──┤
                   ├── Story-4 (models_diff)   ──┼── Story-6 (aggregator + audit.py 集成)
                   └── Story-5 (rate_limit_fp) ──┘
```

Story 2-5 间无依赖，可在 4 个 dev 上并行；Story-1 与 Story-6 是串行卡点。

---

## 8. 待补 / 与外部对齐

以下事项不阻塞 design 落盘，但需后续动作：

1. **PRD 切片回炉**（资深产品经理）：PRD §7.2 Q1-Q4 的最终决策落入下一次 PRD 修订；Design 在 PRD 升至 v1.0 后从 `draft` → `approved`。本 design §10 给出架构推荐答复供 PRD 收敛时参考。
2. **audit-engine design 切片**（资深架构师本人下个 design 切片）：对接 ProbeReport 消费契约、twin-call orchestrator 与 probe-core 的协同。我会在自己的下一个 design 切片中正式 freeze ProbeReport schema。
3. **B2 真源对照 key 与额度锁定**（项目负责人）：probe-core 本身不消耗真源（不调用对照通道），但 detectors-fingerprint 依赖它。在 TES-93 锁定预算前，probe-core 可独立交付与运行。
4. **B3 Web UI 范围确认**（项目负责人）：probe-core 已设计为 CLI 优先 + ProbeReport.to_dict() 序列化稳定，Web UI 加进来时直接消费 JSON，无需重构。
5. **`tests/test_probe_schema_parity.py` 实现细节**（dev 代表）：Story-1 期间与资深开发工程师对齐 schema 断言风格（参考现有 `test_dual_distribution_parity.py` 的字符级断言模式）。

---

## 9. brainstorming 9 步对照表（自验）

| 步 | 内容 | 本文档章节 |
|---|---|---|
| 1. 意图明确 | 把 S1 4 子能力变成可被下游 design 切片消费的"上游基础探测层" | §1 |
| 2. 约束识别 | dual-distribution invariant / KISS / YAGNI / 与现有 13 步审计风格一致 / **PRD §3-§6 已落盘需对齐** | §1.3 + §2.2 缺点 |
| 3. 多候选生成 | A: flat / B: OO orchestrator / C: dataclass + 轻量聚合器 | §2.1-§2.3 |
| 4. 挑战 | 候选 B 违反 dual-dist + 已被 PRD 小拓扑替代；候选 A 无 schema 单点 | §2.2 缺点 / §3.2 不选 A |
| 5. POC 引用 | infra_fingerprint.py / latency_variance.py / perf/runner.py / test_dual_distribution_parity.py | §2.4 |
| 6. 取舍矩阵 | 9 维评分表 | §3.1 |
| 7. 推荐 | 候选 C + 给出"为什么不选 A/B"+ 两阶段并行实现拓扑 | §3.2 + §3.3 |
| 8. 回退 | C 失败回退 A，迁移成本仅 4 模块 return 类型变更 | §2.4 末段 |
| 9. 交付 | ProbeReport 契约 + 6 Story 拆分 + DAG + 工时 + 验收对齐 PRD §6.2 | §4 + §7 + §10 |

---

## 10. 对 PRD §7.2 开放问题的架构推荐答复

PRD §7.2 列了 Q1-Q4 4 个未决问题，本 design 从架构与现有约束角度给出推荐答复。**最终决策权在 PM**，本节是给 PM / PJM 收敛 PRD 时的输入。

### Q1：official 清单同步脚本是否内置？

**PM 提的两个方向**：M1 MVP 不做、靠人工同步；M2 内置 `scripts/sync_official_models.py` 自动抓取。

**架构推荐：M1（不内置）**。原因：

- 厂商文档站点结构月度漂移率高（OpenAI / Anthropic 都在 2026-Q1 重排过 model 文档），自动脚本维护成本 > 人工季度 review 成本；
- CLAUDE.md "Contribution Philosophy" 强反对 speculative：当前已有 4 厂商人工策展机制（`perf/runner.py:VENDOR_MODELS` 已是同样模式），未见用户反馈说"清单过时是阻塞"；
- references/*.json 自带 `catalog_version` + 60 天过期 CI 告警（PRD R1）已足够；
- 真正的"声明 vs 实际"打假能力下沉到 detectors-fingerprint 的 twin-call，那才是高价值；probe-core 的清单 diff 是宏观体检，弱信号容忍漂移。

### Q2：P4 限速探针的"故意触发 4 次"是否需要项目负责人显式确认？

**PM 担忧**：默认开启会在小份额 key 上吃掉额度。

**架构推荐：保留默认开启 + 明显文档化 + `--no-rate-limit-probe` 一键关闭 + `audit.py --probe-only` 退出码 1（warn）的差异化报告**。原因：

- 4 次 burst × max_tokens=1 ≈ 4 token output 总成本（即使 GPT-4o 也不到 0.0002 美元），单 audit 影响可忽略；
- "小份额 key" 是合规审计的常见场景（PRD §2.2 角色「合规 / 风控审计员」），关键不是省 token 而是不留风险痕迹——所以**核心约束是"用户能在不读源码的情况下知道 P4 会发起请求"**，靠 README + audit.py 启动 banner 解决；
- 如果要进一步保守，可在 PRD v1.0 加配置：默认 12+0（仅 baseline，不 burst），用户显式 `--rate-limit-probe-burst` 才打 4 burst。本 design 不强加这个，让 PM 在 PRD 决定。

### Q3：ProbeReport 是否单独输出独立 HTML 卡片？

**PM 倾向**：MVP 嵌入 audit 主报告 + JSON 单独可获取，HTML 单独卡片放 S5 编排切片。

**架构推荐：完全同意**。本 design §4 / §7 Story-6 已按此实现：

- `aggregator.run_probes()` 输出 ProbeReport（dataclass）；
- `scripts/audit.py` Step-0 调用 `reporter.h2(...)` + `reporter.flag(...)` 渲染嵌入主报告；
- ProbeReport.to_dict() → JSON 文件单独可获取（`--output reports/probe.json`）；
- HTML 卡片不在 probe-core 范围内，归 S5 orchestration-report 切片承担（S5 design 已落盘 commit 190f1d3，会消费 ProbeReport JSON）。

### Q4：`--probe-only` 模式独立 CLI 还是 audit.py 子模式？

**PM 倾向**：先走子模式（A2），独立入口由 S5 决定。

**架构推荐：完全同意**。子模式有两个工程优势：

- audit.py 已经是 dual-distribution 的"主入口"（CLAUDE.md "Dual Distribution Model"），独立 CLI 会强制把 dual-distribution 关系扩到第二个文件，复杂度翻倍；
- 用户已习惯 `python scripts/audit.py <url> <key> <model>` 的 3 参数 CLI，加 `--probe-only` 学习成本最低；
- 未来 S5 编排层若要做 `scripts/probe.py` 独立 CLI，可在 S5 design 切片决定，仍是 thin wrapper 调用 `api_relay_audit.probe.run`，无需重构 probe-core 内部。

---

## 11. 对 PRD §6.2 验收清单的工程映射

下表把 PRD §6.2 的 13 条 A1-A13 验收映射到本 design 的 Story 与代码位置，便于 PJM 拆 Story 时一一核对：

| PRD 验收 | 命令 / 行为 | 实现位置（Story） |
|---|---|---|
| A1 模块入口 | `python -c "from api_relay_audit.probe import run; print(run.__doc__)"` | Story-1 + Story-6（`__init__.py`） |
| A2 端到端三态 | `audit.py --probe-only` 退出码 0/1/2 | Story-6 |
| A3 schema 校验 | `python -m api_relay_audit.probe.schema_check` | Story-1（schema_check.py + schema.json） |
| A4 P1 6 分支 mock | `pytest tests/test_probe_reachability.py` | Story-2 |
| A5 P2 5 分支 mock | `pytest tests/test_probe_auth_sniff.py` | Story-3 |
| A6 P3 3 类 diff | `pytest tests/test_probe_models_diff.py` | Story-4 |
| A7 P4 上限保护 | `pytest tests/test_probe_rate_limit.py::test_max_requests` | Story-5 |
| A8 key 不外泄 | `pytest tests/test_probe_no_key_leak.py` | Story-3（聚合到 auth_sniff Story） |
| A9 fail-fast 短路 | exit code 2 + 13 步 step counter=0 | Story-6 |
| A10 与 infra_fingerprint 协同 | ProbeReport.infra_hint vs 独立运行结果一致 | Story-6（aggregator 嵌入 infra_hint） |
| A11 性能 | `time audit.py --probe-only ≤ 10s` | Story-6（两阶段并行） |
| A12 流量 | mitmdump < 50 KB | Story-6（CI mock 抓包断言） |
| A13 master 兼容 | `--skip-probe` 行为与 master@6693aca 一致 | Story-6（dual-dist + diff 单测） |

**B1 / B2** 跨切片验收（与 S2-S5 的对接 + perf-bench-v2 接入）落到 audit-engine 与 perf-bench-v2 design 切片承担，不在本 design 范围。
