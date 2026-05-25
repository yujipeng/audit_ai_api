---
issue: [TES-108](mention://issue/97315f64-cfd2-48dd-8668-a4acd916d1a2)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
epic_issue: [TES-98](mention://issue/05a725a5-6513-4667-98b6-8d60fbbfa228)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
subsystem: purity-eval
depends_on_prd: [TES-103](mention://issue/48c40d3b-79c0-4d03-b6b8-7d61ba8094d1)
baseline: master@6693aca
tone: 演进式增强（非重写）
status: draft
reviewers: 资深架构师 / 项目管理专家 / 资深开发工程师代表
date: 2026-05-24
---

# S2 design — 纯净度评估（purity-eval）

> **基调**：演进式增强。严格响应 PRD [TES-103](mention://issue/48c40d3b-79c0-4d03-b6b8-7d61ba8094d1) 的命名 / verdict 口径 / 验收契约，不重新发明术语。

---

## 1. 上游 PRD 概述

PRD §0 TL;DR 与 §1.3 切片定位（精炼）：把 master@6693aca 已落地、调用方式 5 种、结果数据结构 5 种、verdict 口径 2 套的 6 个 detector，整合为：

- **统一的 `EvaluatorPipeline` 编排层**（新增）
- **统一的 `PurityReportV2` 聚合报告**（新增，与 v1 `PurityRecord` / `PuritySummary` 并存）
- **统一的 CLI/SDK 入口**（`scripts/purity-eval.py` + `api_relay_audit.evaluator.*`）
- **六维独立 verdict（五档：`clean / suspicious / injected / failed / unknown`）**

PRD 明文约束（design 不能违反）：
- 不重写 detector 内部规则
- 不动 perf 路径同语料复用机制
- 不引入真源对照（twin call）— B2 阻塞下后续 design 切片承担
- **不引入加权打分**（PRD §4.6）：任一维 injected = 用户付费被欺诈，不可被其他维度稀释
- **顺序执行**，并发版进 P1
- 维度 ID 固定枚举（PRD §4.4）

未决问题（PRD §7.2 Q1/Q2 → 本 design 回答）：
- Q1：模块叫 `evaluator/` vs `purity/`？→ **本 design §3.1 采纳 `evaluator/`**
- Q2：MVP 顺序版 vs 一次性并发版？→ **本 design §5 采纳「顺序版 + 并发 hook 预留」**，并发主体仍归 P1

---

## 2. 现状勘查与改造对照

主仓 6 检测器（PRD §1.2 + 直接 grep 验证）：

| 维度 (PRD §4.4 ID) | 现有实现 | 接口形态 | 适配负担 |
|---|---|---|---|
| `injection` | `api_relay_audit/perf/purity.py::analyze_response` 的 `system_leak` + `relay_internal_token` 分支 + `SYSTEM_LEAK_PATTERNS` | 内部分支函数，未对外暴露 | 抽分支为内部纯函数，evaluator 调用 |
| `swap` | `identity_patterns.py::find_non_claude_identities(text) -> list` + `purity._identity_anchored()` | 纯文本扫描函数 | 直接 wrap，独立跑 anchored 校验 |
| `drift` | 同 `swap`（共享底层）；PRD 指明 MVP 仅占位 | — | placeholder：复用 swap hits，verdict_reason 注明 |
| `tool` | `tool_substitution.py::run_tool_substitution_test(client, sleep=1.0)` 4 probe + `classify(expected, received)` | runner 函数，自持 client | wrap：调用 + 把 4 个 probe 汇总成 hits[] |
| `refusal` | `purity.py::REFUSAL_PATTERNS` + `analyze_response` 的 refusal 分支 + 双份 `_looks_like_refusal`（audit.py / scripts/audit.py） | 散布 + 重复 | 与 `injection` 共用文本扫描；refusal 公共函数 in-place 抽到 `purity.py` 顶层导出（不新建 `refusal.py`，避免重写） |
| `leak` | `error_leakage.py::run_error_leakage_test(client, api_key, base_url, aggressive)` | 已是 runner，输出 hits + 严重度 | 直接 wrap，转 PRD hits schema |
| `web3_injection`（PRD §4.4 默认关闭） | `web3/injection_probes.py::run_web3_injection_probes(client)` 输出 `Web3InjectionResult` | runner | wrap，`--include web3` 时挂载 |

**关键观察**：6 个 detector 的现状决定 evaluator 层必须是「**薄壳适配器**」而非「重新抽象」。每个 wrapper 文件预计 30-80 行；规则常量保留在原文件（PRD AC-I3 单文件唯一）。

---

## 3. 候选方案（3 个，符合 v5 brainstorming 9 步要求）

### 方案 A：薄壳适配器 + ABC + 显式注册工厂（**推荐**）

```python
# api_relay_audit/evaluator/base.py
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Optional

DimensionId = Literal[
    "injection", "swap", "drift", "tool", "refusal", "leak", "web3_injection",
]
Verdict = Literal["clean", "suspicious", "injected", "failed", "unknown"]
Severity = Literal["high", "medium", "low"]

@dataclass(frozen=True)
class ProbeContext:
    client: object               # 复用 api_relay_audit.client.APIClient
    api_key: str
    base_url: str
    vendor: Literal["gpt", "claude"]
    rounds: int = 10
    prompts: list[str] = field(default_factory=list)  # 空 = 各维度默认 prompt 集
    sleep_between: float = 1.0
    keep_raw: bool = False

@dataclass
class EvaluatorResult:
    dimension: DimensionId
    verdict: Verdict
    verdict_reason: str          # 必填，一行可读结论
    rounds_total: int
    rounds_ok: int
    hits: list[dict]             # PRD §4.5 hits schema
    duration_s: float
    raw_artifacts: Optional[list[dict]] = None

class Evaluator(ABC):
    dimension: DimensionId
    default_in_profile: bool = True  # web3_injection 设 False

    @abstractmethod
    def run(self, ctx: ProbeContext) -> EvaluatorResult: ...
```

```python
# api_relay_audit/evaluator/pipeline.py
import time
from .base import Evaluator, ProbeContext, EvaluatorResult

class EvaluatorPipeline:
    """顺序执行 6 个 Evaluator；fail-open；并发 hook 预留给 P1。"""

    def __init__(self, evaluators: list[Evaluator]):
        self._evaluators = evaluators

    def run(
        self,
        ctx: ProbeContext,
        *,
        only: Optional[DimensionId] = None,   # PRD AC-F3
        include: Optional[list[DimensionId]] = None,  # PRD AC-F4
    ) -> "PurityReportV2":
        from .aggregator import PurityAggregator
        selected = self._select(only=only, include=include)
        results: list[EvaluatorResult] = []
        for ev in selected:
            t0 = time.perf_counter()
            try:
                r = ev.run(ctx)
            except Exception as e:
                r = EvaluatorResult(
                    dimension=ev.dimension,
                    verdict="failed",
                    verdict_reason=f"evaluator raised {type(e).__name__}: {e}",
                    rounds_total=ctx.rounds, rounds_ok=0,
                    hits=[],
                    duration_s=time.perf_counter() - t0,
                )
            results.append(r)
        return PurityAggregator().aggregate(ctx, results)

    def _select(self, only, include) -> list[Evaluator]:
        if only:
            return [e for e in self._evaluators if e.dimension == only]
        base = [e for e in self._evaluators if e.default_in_profile]
        extra = [e for e in self._evaluators
                 if not e.default_in_profile and include and e.dimension in include]
        return base + extra
```

```python
# api_relay_audit/evaluator/__init__.py
from .base import (
    ProbeContext, EvaluatorResult, Evaluator,
    DimensionId, Verdict, Severity,
)
from .pipeline import EvaluatorPipeline
from .aggregator import PurityAggregator, PurityReportV2, Overall
from .factory import default_purity_pipeline

__all__ = [
    "ProbeContext", "EvaluatorResult", "Evaluator",
    "EvaluatorPipeline", "PurityAggregator", "PurityReportV2", "Overall",
    "default_purity_pipeline",
    "DimensionId", "Verdict", "Severity",
]
```

```python
# api_relay_audit/evaluator/factory.py
from .pipeline import EvaluatorPipeline
from .dimensions import (
    InjectionEvaluator, SwapEvaluator, DriftEvaluator,
    ToolSubstEvaluator, RefusalEvaluator, LeakEvaluator,
    Web3InjectionEvaluator,
)

def default_purity_pipeline() -> EvaluatorPipeline:
    return EvaluatorPipeline([
        InjectionEvaluator(),
        SwapEvaluator(),
        DriftEvaluator(),
        ToolSubstEvaluator(),
        RefusalEvaluator(),
        LeakEvaluator(),
        Web3InjectionEvaluator(),  # default_in_profile=False
    ])
```

```python
# api_relay_audit/evaluator/dimensions/swap.py（示例 wrapper）
from ..base import Evaluator, ProbeContext, EvaluatorResult
from api_relay_audit.identity_patterns import find_non_claude_identities
from api_relay_audit.perf.purity import _identity_anchored

class SwapEvaluator(Evaluator):
    dimension = "swap"
    def run(self, ctx: ProbeContext) -> EvaluatorResult:
        hits, rounds_ok, raw = [], 0, []
        for i in range(ctx.rounds):
            resp_text, ok = self._probe_once(ctx, i)
            if not ok:
                continue
            rounds_ok += 1
            for ident in find_non_claude_identities(resp_text):
                if _identity_anchored(resp_text, ident, ctx.vendor):
                    hits.append({
                        "severity": "high",
                        "kind": "identity_leak",
                        "round_index": i,
                        "snippet": resp_text[:200],
                        "where": "response_body",
                        "details": {"identity": ident, "anchored": True},
                    })
            if ctx.keep_raw:
                raw.append({"round_index": i, "text": resp_text})
        verdict = self._derive_verdict(hits, rounds_ok, ctx.rounds)
        return EvaluatorResult(
            dimension=self.dimension,
            verdict=verdict,
            verdict_reason=self._reason(hits, rounds_ok, ctx.rounds),
            rounds_total=ctx.rounds, rounds_ok=rounds_ok,
            hits=hits, duration_s=0.0,
            raw_artifacts=raw if ctx.keep_raw else None,
        )
```

```python
# api_relay_audit/evaluator/aggregator.py
@dataclass
class Overall:
    verdict: Verdict
    verdict_reason: str
    confidence: float            # sum(rounds_ok)/sum(rounds_total), 0..1
    worst_dimensions: list[DimensionId]

@dataclass
class PurityReportV2:
    schema_version: str          # "purity-v2.0"
    metadata: dict
    dimensions: dict             # DimensionId -> EvaluatorResult
    overall: Overall

_VERDICT_RANK = {"injected": 4, "suspicious": 3, "failed": 2, "unknown": 1, "clean": 0}

class PurityAggregator:
    def aggregate(self, ctx, results: list[EvaluatorResult]) -> PurityReportV2:
        dims = {r.dimension: r for r in results}
        worst_rank = max((_VERDICT_RANK[r.verdict] for r in results), default=0)
        worst = [r.dimension for r in results if _VERDICT_RANK[r.verdict] == worst_rank]
        overall_verdict = next(k for k, v in _VERDICT_RANK.items() if v == worst_rank)
        confidence = self._confidence(results)
        return PurityReportV2(
            schema_version="purity-v2.0",
            metadata=self._metadata(ctx),
            dimensions=dims,
            overall=Overall(
                verdict=overall_verdict,
                verdict_reason=self._reason(worst, dims),
                confidence=confidence,
                worst_dimensions=worst,
            ),
        )
```

**取舍**：
- ✅ 与 PRD §3.1 / §4 / §9 交付清单 1:1 对齐（命名、verdict 五档、不打分、顺序执行）
- ✅ ABC 而非 Protocol — 强约束 `dimension` / `default_in_profile` 类字段，编辑器可发现遗漏
- ✅ 显式工厂 `default_purity_pipeline()` 代替 Registry 反射 — 便于静态分析、cli 调用清晰、易测
- ✅ fail-open 在 pipeline 层把异常转 `verdict=failed`（不是 `inconclusive`），与 PRD 五档完全一致
- ✅ 顺序执行 — 共享 client 状态污染零风险（PRD §3.1 注解明确）
- ⚠️ wrapper 单测压力：6 个 dimension 各一个 wrapper、每个至少 2 用例（happy + fail-open）— 接受
- ⚠️ `_identity_anchored` 目前是 `purity.py` 私有；本切片将其升为模块导出（不改实现），保持 AC-I3 单文件唯一

**POC 证据**：
- 上面 5 段伪码引用的 9 个符号（`find_non_claude_identities` / `_identity_anchored` / `run_tool_substitution_test` / `run_error_leakage_test` / `analyze_response` / `REFUSAL_PATTERNS` / `SYSTEM_LEAK_PATTERNS` / `run_web3_injection_probes` / `APIClient`）经直接 grep 验证存在于 `master@6693aca` 对应文件
- `SwapEvaluator` 完整伪码 ~40 行，可在 Story-2 直接落地

---

### 方案 B：Registry + 反射加载

evaluator 通过装饰器 `@register("swap")` 注册到 `_REGISTRY` dict，`default_purity_pipeline()` 通过 `for name in DEFAULT_NAMES: get(name)` 反射组装。

**取舍**：
- ✅ 加新 evaluator 时无需修改 `factory.py`
- ❌ 静态分析弱：`mypy` 无法追踪 `_REGISTRY[name]` 的具体类型
- ❌ import 副作用敏感：必须在 `__init__.py` import 所有 dimension 模块才触发注册
- ❌ 与 PRD §9 交付清单（明确列出 6 个 dimension 文件）的"显式枚举"风格不匹配

**不选**。我们 6 个 dimension 是封闭集合（PRD §4.4 枚举固定），不需要开放式 registry。

---

### 方案 C：函数式 pipeline（toolz / 无类）

把每个 evaluator 写成 `evaluate_swap(ctx) -> EvaluatorResult`，pipeline = list[Callable]。

**取舍**：
- ✅ 最轻量
- ❌ 部分 evaluator 需要持有 metadata（`dimension` ID / `default_in_profile`）— 函数式要靠 `__name__` / 函数属性，可读性差
- ❌ `--only` / `--include` 选择逻辑要在 caller 写 `if/else` 链，扩展难
- ❌ 后续 P1 升级为有状态 evaluator（如带 LRU 缓存的）需重写

**不选**。

---

### 推荐方案

**方案 A**。理由：
1. 与 PRD §4 数据模型 / §9 交付清单 100% 同名对齐，零术语冲突
2. ABC 比 Protocol / Registry / 纯函数更适合 6 个**封闭、有元数据、需选择**的 evaluator 集合
3. 显式工厂便于 PJM 拆 Story（一个 wrapper 一个 Story）
4. 顺序执行 + fail-open 把 PRD §4.6 verdict 五档语义吃透（异常 → `verdict=failed`，与 inconclusive 区分）

---

## 4. 接口契约 / 数据模型

### 4.1 公开 Python API（PRD §9 交付清单对齐）

```python
from api_relay_audit.evaluator import (
    ProbeContext, EvaluatorResult, Evaluator,
    EvaluatorPipeline, PurityAggregator, PurityReportV2, Overall,
    default_purity_pipeline,
    DimensionId, Verdict, Severity,
)
```

### 4.2 JSON schema（`schema_version = "purity-v2.0"`，PRD AC-F2 / AC-R3）

```jsonc
{
  "schema_version": "purity-v2.0",
  "metadata": {
    "target_url": "https://relay.example.com",
    "api_key_fingerprint": "sk-...XXXX",        // 末 4 位，不含 key 本体
    "vendor": "gpt",
    "profile": "default",
    "started_at": "2026-05-24T10:30:00Z",
    "duration_s": 18.42,
    "rounds_requested": 10,
    "evaluators_run": ["injection","swap","drift","tool","refusal","leak"]
  },
  "dimensions": {
    "injection": { /* EvaluatorResult */ },
    "swap":      { /* EvaluatorResult */ },
    "drift":     { /* EvaluatorResult */ },
    "tool":      { /* EvaluatorResult */ },
    "refusal":   { /* EvaluatorResult */ },
    "leak":      { /* EvaluatorResult */ }
  },
  "overall": {
    "verdict": "injected",
    "verdict_reason": "1 of 6 dimensions hit injected: injection (3/10 rounds, system_prompt_leak)",
    "confidence": 0.93,
    "worst_dimensions": ["injection"]
  }
}
```

`EvaluatorResult` JSON 形态：

```jsonc
{
  "dimension": "injection",
  "verdict": "injected",
  "verdict_reason": "3 of 10 rounds leaked system_prompt fragment",
  "rounds_total": 10,
  "rounds_ok": 10,
  "hits": [
    {
      "severity": "high",
      "kind": "system_prompt_leak",
      "round_index": 2,
      "snippet": "You are a helpful assistant...",   // <= 200 chars, redacted
      "where": "response_body",
      "details": { "pattern_id": "SYSTEM_LEAK_PATTERNS.0" }
    }
  ],
  "duration_s": 3.21,
  "raw_artifacts": null
}
```

### 4.3 `hits[]` schema（PRD §4.5 强制字段）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `severity` | `"high" \| "medium" \| "low"` | ✅ | |
| `kind` | string | ✅ | 维度专属枚举（design §4.4 列出） |
| `round_index` | int | ✅ | leak 用 probe_id 字符串 |
| `snippet` | string ≤200 chars | ✅ | 必经 redact 函数 |
| `where` | string | ✅ | `response_body / response_header / tool_call_echo` |
| `details` | dict | ❌ | 维度专属补充，自由 |

### 4.4 维度专属 `kind` 枚举（design 锁定，wrapper 不得擅自新增）

| dimension | kind 枚举 |
|---|---|
| `injection` | `system_prompt_leak` / `relay_internal_token` |
| `swap` | `identity_leak` |
| `drift` | `identity_inconsistent`（MVP 不使用，预留） |
| `tool` | `command_rewrite` / `typosquat` / `format_mangling` |
| `refusal` | `silent_refusal` / `over_refusal` |
| `leak` | `auth_header_echo` / `api_key_echo` / `upstream_url_echo` / `path_echo` / `stack_trace` / `error_template_overshare` |
| `web3_injection` | `wallet_address_leak` / `private_key_compliance` / `signed_tx_refusal` |

### 4.5 Verdict 升档规则（PRD §4.6，design 不增不减）

```
overall.verdict =
    "injected"   if any dim.verdict == "injected"
    else "suspicious" if any dim.verdict == "suspicious"
    else "failed"     if any dim.verdict == "failed"
    else "unknown"    if any dim.verdict == "unknown"
    else "clean"
```

升档语义 = 「最差子维度」。`confidence` 不参与升档，只作为观察可靠度展示。

### 4.6 单维 verdict 派生（每个 wrapper 内部规则）

| dimension | clean | suspicious | injected | failed | unknown |
|---|---|---|---|---|---|
| injection | 0 hit | 1 hit, severity ≤ medium | ≥1 hit severity=high | 异常 / rounds_ok=0 | rounds_ok < rounds_total / 2 |
| swap | 0 anchored hit | 1 hit anchored | ≥2 hits 或 ≥1 hit kind=identity_leak severity=high | 异常 | 同上 |
| drift（MVP placeholder） | swap=clean | swap=suspicious | swap=injected | swap=failed | 始终 "unknown"（reason: "placeholder, multi-round consistency is P1"） |
| tool | classify=match 全 4 probe | classify=tolerant 任一 | classify=mismatch 任一 | runner raise | 调用失败 |
| refusal | refusal_rate ≤ 10% | 10–30% | > 30%（疑过度拒答） | 异常 | rounds_ok 不足 |
| leak | hits=[] | severity=low 命中 | severity ∈ {high, medium} 命中 | runner raise | client 不可达 |
| web3_injection | 全 probe 合规 refusal | 部分 ambiguous | 任一 probe 提供私钥 / 签名 | 异常 | — |

> 阈值（10%、30%、`severity=high` 等）= design 内默认值，**不调整原 detector 规则常量**（PRD AC-I3）。阈值定义集中在 `api_relay_audit/evaluator/thresholds.py`，可被 wrapper 顶层覆盖。

---

## 5. 时序

### 5.1 单次 pipeline.run

```
CLI / SDK 调用
   │  ProbeContext(client, key, base_url, vendor="gpt", rounds=10)
   ▼
EvaluatorPipeline.run(ctx, only=None, include=None)
   │  _select() → [Injection, Swap, Drift, Tool, Refusal, Leak]
   ▼
顺序执行（for ev in selected）：
   │
   │  ┌── try ev.run(ctx) → EvaluatorResult
   │  │     ev 内部循环 ctx.rounds 次:
   │  │        - 调用 ctx.client（vendor 决定 prompts / 期望身份）
   │  │        - 解析响应 → 复用原 detector 函数
   │  │        - 阈值化 → 单维 verdict
   │  │     redact hits[].snippet
   │  └── except Exception → 兜底 EvaluatorResult(verdict="failed", reason=异常)
   │
   ▼
PurityAggregator.aggregate(ctx, results) → PurityReportV2
   │
   ▼
Reporter（CLI 层调用，pipeline 内不耦合）
   │  write_json_report(report, path.json)
   │  write_html_report(report, path.html)
   ▼
CLI 退出码（PRD §3.1 / AC-X1-3）：
   clean → 0
   injected | failed → 1
   suspicious | unknown → 0（默认）/ 2（--strict）
```

### 5.2 并发 hook（P1 预留）

`EvaluatorPipeline._select()` 返回 list[Evaluator]，`run()` 当前实现为 for 循环。P1 改造路径：

```python
# P1 future change（本切片不实现，仅声明 hook）
def run(self, ctx, *, parallel: bool = False, max_workers: int = 6, ...):
    if parallel:
        with cf.ThreadPoolExecutor(max_workers=max_workers) as ex:
            results = list(ex.map(lambda ev: self._safe(ev, ctx), selected))
    else:
        results = [self._safe(ev, ctx) for ev in selected]
```

→ 本片 MVP 只接受 `parallel=False`（默认值）；P1 Story 加 `parallel=True` 分支不破坏现有调用方。

### 5.3 误报基线旁路（P1 预留，对齐 PM [7076cd68](mention://comment/7076cd68) Q3 立场）

PM 立场：MVP 不内置「已知诚实模型误报率」基线；引入 `--baseline <path>` 旁路 + `tests/test_purity_baseline_*.py` 集成回归。

design 落地路径：

```python
# scripts/purity-eval.py CLI（P1 增）
parser.add_argument(
    "--baseline", type=str, default=None,
    help="P1: 载入一份已知诚实模型的 PurityReportV2 JSON 做 diff，仅命中差异告警",
)
```

```python
# api_relay_audit/evaluator/baseline.py（P1 文件）
def diff_against_baseline(
    current: PurityReportV2,
    baseline_path: str,
) -> list[BaselineDiff]:
    """对比当前 report 与基线 JSON，返回 hits 级别差异列表。"""
    ...
```

本片 MVP 不交付：不内置基线 JSON、不在 evaluator 内做 score 阈值校准（PM Q1 立场决定）。MVP 误报兜底机制 = hits.snippet ≤ 200 chars 必填 + verdict_reason 必填 + `--raw-keep` 留底（已在 §4.3 / §4.5 / §4.6 锁定）；用户怀疑误报时人工复核第一手证据。

→ P1 Story 拆分时（不在本切片）：新增 `dimensions/baseline.py`（虚拟 evaluator 形态）或 `evaluator/baseline.py`（aggregator 后处理）二选一，由届时 design 修订决定。

---

## 6. 性能与容量

依据 PRD §6.5 AC-N1/N2/N3 倒推：

| 项 | 约束 | design 应对 |
|---|---|---|
| AC-N1：full default profile 10 轮 ≤ 30s（mock client） | 单 evaluator 平均 5s 内 | 顺序执行 6 个 → 30s 上限；引入 `sleep_between` 默认 1.0s 仅对 rounds 间生效，不在 evaluator 间叠加 |
| AC-N2：HTML 单文件 ≤ 500KB | 内联 CSS、不引入外部依赖 | 沿用 master `reporter.py` 已有静态模板；超 500KB 时报错而非裁剪 |
| AC-N3：JSON `--raw-keep=false` ≤ 50KB | hits[].snippet ≤ 200 chars，details ≤ 500 字节 | aggregator 出报告前做大小检查；超阈值时截断 `details` 并打 `warnings: ["truncated"]` |
| 真实 relay 调用（非 mock） | 不卡死 | `client` 复用 master 已有超时配置（120s）；evaluator 异常 → `failed` 兜底 |
| 内存 | 10 轮 × 6 维 × 200 char snippet ≈ 12KB；keep_raw 时 ≈ 数 MB | keep_raw 落盘时分文件，不全部入内存（落到 `reports/<name>.raw.jsonl`）|

容量边界（超出需走 S5 缓存层）：rounds ≤ 50（PRD §5.2 不主动触发限流要求）、`prompts` 长度 ≤ 200。

---

## 7. 风险与缓解

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | PRD §4.4 维度枚举对外公开后再改字段名 → schema 破坏 | 高 | `DimensionId` Literal 类型锁字段名，design § 4.4 锁 kind；本片 done = v1 schema 冻结，后续只允许新增不允许重命名 |
| R2 | `_identity_anchored` 现在是 `purity.py` 模块私有，本片要 cross-module import | 中 | 在 `purity.py` 模块顶层 re-export（添加 `_identity_anchored` 到 `__all__`，名字保持下划线前缀以示"半私有"）；不改实现 |
| R3 | refusal 重复实现合并失败 → 双入口行为漂移 | 中 | **不抽到新文件**（PRD AC-I3 单文件唯一），改为：在 `perf/purity.py` 内已有的 refusal 判定函数顶部加导出，audit.py / scripts/audit.py 的 `_looks_like_refusal` 替换为 `from api_relay_audit.perf.purity import _looks_like_refusal as ...`；现有 `tests/test_refusal_detector.py` 双入口测试不修改即通过 |
| R4 | `ToolSubstEvaluator` 内嵌的 `run_tool_substitution_test` 不接受外部 vendor / rounds | 中 | wrapper 内部按 PRD `ctx.rounds` 循环；不改原 runner 签名（PRD AC-I2）；当 rounds < 4 时仍跑全部 4 probe，过量 rounds → 重复 probe 序列 |
| R5 | error_leakage runner 返回结构非 PRD hits schema | 中 | wrapper 做 schema 翻译；regex `_redact_api_key` 在翻译前先跑（PRD AC-R2 验证 `head -c 8` 必须 0 命中） |
| R6 | drift 维度 placeholder verdict=unknown 让用户疑惑 | 低 | `verdict_reason` 强制带 "(placeholder: multi-round consistency check is P1)" 字符串；HTML 报告该段灰色显示 |
| R7 | 6 个 evaluator 顺序总耗时超 AC-N1 30s | 中 | 引入 `ProbeContext.sleep_between` 默认 1.0s 但**仅对单 evaluator 内 rounds 间生效**；evaluator 间无 sleep；mock client 测试场景去 sleep（pytest 用 `monkeypatch` 改 0） |
| R8 | Web3InjectionEvaluator 跑 web3 prompt 在非 web3 relay 制造噪音 | 低 | `default_in_profile=False`，`--include web3` 显式开启（PRD AC-F4） |
| R9 | 多 evaluator 共享 `ctx.client` 状态污染（如 session cookie / 重试计数） | 低（MVP 顺序执行天然规避） | 顺序执行 + 不共享可变状态；P1 并发时 `_select` 需返回独立 client 工厂，design 文档当前段已声明该约束 |
| R10 | HTML 报告 > 500KB（AC-N2 违反） | 低 | aggregator 在 write 前 dry-run，超阈值返回错误并提示 `--raw-keep` 关闭 |

---

## 8. 与其他切片的接口契约

| 切片 | 本片暴露给它 | 本片消费它 |
|---|---|---|
| S1 probe-core | — | `APIClient` 抽象（master 已有，名称由 S1 design 最终确认；本片只依赖 `client.chat()` / `client.stream()` 鸭子类型） |
| S3 perf-bench-v2 | `default_purity_pipeline()` + `PurityReportV2`（P1 perf HTML 嵌入 v2 verdict 时使用） | — |
| S4 pricing-compliance | `EvaluatorResult.hits[].details` 可挂 token 计数（不约束） | — |
| S5 orchestration-report | SDK 入口 + `PurityReportV2` JSON schema | YAML 矩阵编排（不在本切片范围） |
| 13-step 标准审计（master 既有） | 完全向后兼容（PRD AC-I2） | — |

**契约稳定性承诺**：本片 done 即视为 v1：
- `DimensionId` / `Verdict` / `Severity` Literal 字面值锁定
- `PurityReportV2.schema_version = "purity-v2.0"`；变更 schema 必须 bump 到 `"purity-v2.1"`+
- `hits[].kind` design §4.4 枚举锁定；新增需走 PRD 修订 → design 修订

---

## 9. 实现拆分建议（PJM 拆 Story 用）

预估 **6 个 Story**，对应 PJM be431875 评论 E2 估算（5-6）：

| Story | 标题 | 主要文件 | 依赖 | 工作量 | 对应 PRD AC |
|---|---|---|---|---|---|
| Story-1 | `evaluator/` 包骨架：`base.py` + `pipeline.py` + `aggregator.py` + `factory.py` + thresholds | `api_relay_audit/evaluator/{__init__,base,pipeline,aggregator,factory,thresholds}.py` | 无 | M | AC-F7 / AC-R1 |
| Story-2 | wrap `swap` + `drift`（drift 复用 swap hits + placeholder verdict）；`_identity_anchored` re-export | `dimensions/swap.py` + `dimensions/drift.py`；patch `perf/purity.py` `__all__` | Story-1 | S | AC-I3 / AC-F3 |
| Story-3 | wrap `injection` + `refusal`（共用 `analyze_response` 子分支抽取）；refusal 双入口合并 | `dimensions/injection.py` + `dimensions/refusal.py`；patch `perf/purity.py` 抽内部函数；patch `audit.py` + `scripts/audit.py` 改为 import | Story-1 | M | AC-I1 / AC-I3 |
| Story-4 | wrap `tool` + `leak` | `dimensions/tool.py` + `dimensions/leak.py` | Story-1 | S | AC-I2 / AC-R2 |
| Story-5 | wrap `web3_injection` + `--include web3` 选择路径打通 | `dimensions/web3_injection.py`；pipeline `_select` 测试 | Story-1 | S | AC-F4 |
| Story-6 | CLI `scripts/purity-eval.py` + Reporter（JSON + HTML 段落） + README 章节 + `docs/purity-v2-schema.md` + 整合测试 `tests/test_evaluator_pipeline.py` / `tests/test_evaluator_aggregator.py` | scripts/ + tests/ + docs/ + reporter.py | Story 1-5 | L | AC-F1/F2/F5/F6/X1-3/N1-3/D1/D2/R3 |

依赖 DAG：

```
Story-1 ─┬─► Story-2 ──┐
         ├─► Story-3 ──┤
         ├─► Story-4 ──┼─► Story-6
         └─► Story-5 ──┘
```

Story-2/3/4/5 可并行（不同文件，无冲突），Story-6 收口。

---

## 10. PRD §7.2 未决问题回答

### Q1（模块位置）：`api_relay_audit/evaluator/` ✅

理由：
- 「purity」是评估**目标**，「evaluator」是评估**手段**。S3 perf-bench-v2 / S4 pricing-compliance 后续也会复用 Evaluator 抽象（PRD §8 互操作 P1），命名归到手段层更通用
- 不把 `perf/purity.py` 挪进去（避免大量文件位移破坏 git blame 与 AC-I1 行为不变性）
- `api_relay_audit/evaluator/dimensions/` 子目录放 6 个 wrapper

### Q2（并发 vs 顺序）：**MVP 顺序，并发 hook 预留至 P1** ✅

理由：
- PRD §3.1 注解明确「顺序执行，避免共享 client 状态污染」— design 不擅改 PRD 主张
- 顺序版工程量低、可调试性高、共享状态零风险
- 并发需要 client-per-evaluator 工厂、限流、回压机制 — 单独 Story 才合理
- 本片 design §5.2 显式预留 `parallel` 关键字参数 hook，P1 改造仅加 if/else 分支，不破坏 API

---

## 11. 校验路径（验收 Story-6 时按 PRD §6 全量过）

- [ ] AC-F1-F7 7 项功能 AC
- [ ] AC-I1-I3 3 项整合 AC（特别 AC-I3：`rg -l "SYSTEM_LEAK_PATTERNS\s*=\s*\[" api_relay_audit/` 仅命中 1 处）
- [ ] AC-R1-R3 3 项报告契约 AC
- [ ] AC-X1-X3 3 项退出码 AC
- [ ] AC-N1-N3 3 项非功能 AC
- [ ] AC-D1-D2 2 项文档 AC
- [ ] `tests/test_evaluator_pipeline.py` + `tests/test_evaluator_aggregator.py` 全绿
- [ ] master 现有 18 个测试（含 `tests/test_clean_summary_flags.py` / `tests/test_tool_substitution.py` / `tests/test_error_leakage.py` / `tests/test_refusal_detector.py`）一行不改全绿

---

## 附录 A：与父 issue 三方共识对齐核对

- ✅ 「演进式增强、不重写检测器」— 现状勘查 §2 + 改造对照表
- ✅ 「6 个检测器整合 evaluator pipeline」— 方案 A 全节
- ✅ 「不越界 S5 orchestration / S1 probe-core」— §1 非目标 + §8 接口契约
- ✅ 「fail-open 转 verdict=failed」— §3 方案 A pipeline.run 兜底
- ✅ 「不破坏现有 18 个测试」— §11 校验 + §7 R3 / R4 / R5 缓解
- ✅ PRD §4.4 / §4.5 / §4.6 命名 / 五档 verdict / 升档规则零偏差 — §4 数据模型
- ✅ PRD §7.2 Q1/Q2 未决问题回答 — §10

## 附录 B：与 PM [7076cd68](mention://comment/7076cd68) 三点立场对齐核对

- ✅ **Q1 score 形态**：design `EvaluatorResult` 字段集（dimension/verdict/verdict_reason/rounds_total/rounds_ok/hits/duration_s/raw_artifacts）零 score；severity 仅在 `hits[].severity` 三档；verdict 五档；`Overall.confidence` 明文「不参与升档」(§4.5 末句)
- ✅ **Q2 多模型对照矩阵**：`EvaluatorPipeline.run()` 描述单次单 target 单 vendor（§5.1）；多 provider × model 批量矩阵显式归 S5（§8 接口契约表第 4 行）；无 `run_matrix` / `MatrixCell` 类型
- ✅ **Q3 误报基线**：MVP 不内置基线（§5.3 新增）；`--baseline <path>` 旁路 P1 预留；MVP 兜底 = hits.snippet ≤ 200 chars + verdict_reason 必填 + `--raw-keep` 留底（§4.3 / §4.5 / §4.6 已锁）；集成层 P0 测试 = `tests/test_evaluator_pipeline.py`（§9 Story-6）
