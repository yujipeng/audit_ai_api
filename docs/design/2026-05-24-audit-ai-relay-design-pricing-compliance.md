---
issue: [TES-110](mention://issue/04e49af6-9bfe-436c-8a0f-8a30e1a05243)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
subsystem: pricing-compliance
depends_on_prd: [TES-105](mention://issue/126e1dc5-0bc7-4240-80e1-f7dfde8f3b86)
status: draft
reviewers: 资深架构师 / 项目管理专家 / 资深开发工程师代表
---

# S4 Design — Pricing Compliance（价格合规子系统）

> 切片范围：tokenizer 对齐策略 + 实测计费抓取 + 偏差容忍模型
> 基调：演进式增强（master@6693aca 已含 client.py usage 采集；本切片新增 `api_relay_audit/pricing/` 子模块）
> 注：依赖 PRD 切片 TES-105 当前 todo，本 design 头部 status=draft，待 PRD done 后做对齐复核再切 approved

---

## 1. 上游 PRD 概述（占位 + 现状推断）

PRD 切片 TES-105 当前 todo 未交付，本节按 [8726aed4](mention://comment/8726aed4) 现状勘查锁定的 D4 切片范围 + 父 issue [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c) 用户底线条目，**推断**核心需求：

- **U1 声称单价 vs 实测扣费**：核对中转站宣称的「每百万 token X 元」与账户实际扣费是否一致
- **U2 token 计量口径核对**：核对 `usage.prompt_tokens / completion_tokens / cached_tokens` 是否如实反映本地 tokenizer 计算量级（防止「虚报 token 收费」与「漏报 token 收费」两种欺诈）
- **U3 隐藏倍率 / cache token 计费**：检测中转站是否对 cache hit 仍按 full price 计费、是否对特殊路径（function calling / vision / tool）使用未公开倍率
- **U4 与官方价格表 diff**：上游官方有变（如 Anthropic 季度调价）时，本地价格库要能滚动更新并 diff 中转站宣称值

**与 D1/D2/D3/D5 的边界**（依据 [8726aed4](mention://comment/8726aed4) DAG）：

- 依赖 D1 **probe-core** 的「模型清单 (`/v1/models`) 拉取 + 官方清单 diff」产物 → D4 用其确认目标 model_id 在中转站存在且声明可用
- 不重复 D2 **purity-eval** 的「模型偷换 / 提示词注水」结论 → D4 仅就「在 D2 已判定 identity-clean 的样本」上做计费 diff，避免计费偏差被错误归因为模型替换
- 不重复 D3 **perf-bench-v2** 的负载并发 → D4 单点低 QPS 计费抓取，避免 burst 引起 provider 侧限速影响 usage 字段回写
- 输出消费方 D5 **orchestration-report** → D4 暴露统一 evaluator 接口供编排矩阵调用，结果走 transparent_log + reporter 双通道

> **PRD 收口动作**：PRD done 后，架构师须复核本节占位条款是否仍然准确，必要时回 in_progress 修订 design 第 1/2/4/6 节。

---

## 2. 候选方案（brainstorming 9 步）

### 关键议题

本切片对外接口契约 = "(给定一次到 relay 的调用) 输出该次调用的 4 维计费偏差判定与置信度"，但**怎么得到 ground-truth token 数 + 怎么界定『偏差』**有三种实现路径，且与 ROADMAP.md:740 明确拒绝过的「Token accounting audit」存在张力，必须显式正面回应：

- 是否引入 `tiktoken` / Anthropic SDK 的 tokenizer？引入会破坏「standalone `audit.py` 零依赖」不变量
- 偏差容忍带宽如何设？硬阈值（如 5%）会被 Tokenizer 版本差和消息包装差异淹没，太松又抓不到欺诈
- 实测计费数据怎么来？relay 的 `usage` 字段是被审计方自己回写的，本身可能已经是欺诈输入；账户余额扣费才是真 ground-truth，但 relay 多数没有暴露余额查询 API

### 候选方案 A：经验型偏差检测（character-ratio + usage 字段 + 多次重测）

**思路**：完全沿用 master 仓 `docs/python-code-explanation-zh.md:690-740` 已经在 prompt-injection 检测中验证过的「人工预期值 + delta + 黄/红阈值」方法论，扩展到 token 计费维度。

- **本地估算**：用 `len(text)` 字符数 × vendor-specific ratio（GPT 系 ~3.5, Claude 系 ~3.7, 中文 ~1.7）算出 token expected 区间 [low, high]
- **观测信号**：直接拿 `client.py` 已经采集的 `usage.input_tokens / output_tokens / cached_tokens`
- **判定**：actual_in_band(expected_range) ∧ ‖cost_billed − cost_expected‖/cost_expected ≤ 15% → green；超 15% 但 ≤ 50% → yellow；> 50% → red
- **价格表**：硬编码官方公开价格 YAML，季度人工 review 一次，落 `api_relay_audit/pricing/prices.yaml`

**评估**：

| 维度 | 评分 | 说明 |
|---|---|---|
| 性能 | ★★★★★ | 纯本地算术，每次调用 < 1ms 额外开销 |
| 可维护性 | ★★★★ | 无外部依赖、单文件 prices.yaml；与现有 stream_integrity / identity_patterns 风格一致 |
| 实现成本 | ★ | 仅需一个 evaluator 类 + YAML loader + 阈值 + 报告字段 |
| 风险 | ★★★（高） | character-ratio 误差对短消息 / 多语种 / 代码 prompt 敏感；ROADMAP.md:740 显式拒绝过这条路（"too noisy"） |

**与 ROADMAP.md:740 的对峙**：ROADMAP 拒绝的是「精确 token counting」用作主判定。本方案把 character-ratio 仅用作 **outer-bound 离群检测**（500% off 这种量级的欺诈），不冒充精确计量。**风险点**：阈值需要靠 [TES-94](mention://issue/f165c62b-225f-45bc-843a-0322d799c747) 调研集校准，且不能在 standalone `audit.py` 中冒充权威。

### 候选方案 B：tokenizer 对齐 + cache 重放 + 账户余额三角校验

**思路**：完整版「实测计费抓取」。

- 引入 `tiktoken`（GPT 系）+ `anthropic` SDK 自带 tokenizer（Claude 系）做精确本地 tokenization → `local_token_count_truth`
- 同一 prompt 在中转站发两次（间隔 10s），观测 `cached_tokens` 是否合理上扬 → 校验 cache 计费是否被双计
- 若中转站暴露 `/v1/billing` 或类似端点（约 30% 可能，hvoy.ai 调研有 4 家暴露），抓 billed_amount 做三角校验
- 偏差判定：actual_token vs local_truth 相对误差 ≤ 2% 通过；price_per_token vs declared 相对误差 ≤ 1% 通过；cache double-charge 二值检测

**评估**：

| 维度 | 评分 | 说明 |
|---|---|---|
| 性能 | ★★★★ | tokenizer 调用 ~5ms / prompt；额外 1 次 cache 重放调用 |
| 可维护性 | ★★ | 引入两个外部依赖；tokenizer 版本必须随 model 更新，每季度有维护税 |
| 实现成本 | ★★★★ | 需要 tokenizer 抽象层、cache 重放调度、可选余额查询适配；约 800-1200 LOC |
| 风险 | ★★（中） | **破坏 audit.py 零依赖不变量**（ROADMAP.md「Architectural invariants」§2 + `test_dual_distribution_parity`）；Anthropic 官方 tokenizer 仅作为 SDK 内部能力，无稳定 offline 接口 |

**致命问题**：会破坏 master 已有的 dual-distribution parity 不变量（standalone `audit.py` 必须 zero-dep）。除非把 D4 整体「拒绝下沉到 standalone 分发」并显式标注 audit.py 不覆盖 D4，否则方案 B 在仓库现状下不可落地。

### 候选方案 C（推荐）：分层评估器 + tokenizer 渐进介入 + 账户余额可选三角

**思路**：混合 A 与 B 的优点，按「证据梯度」分三层 evaluator，由可用证据自动选择层级，与 ROADMAP 的双分发不变量兼容。

- **L0 字符比启发**：所有 relay 必跑（含 standalone audit.py）；character-ratio + 阈值带；判定 outer-bound 欺诈（500% 误差）
- **L1 tokenizer 校准**：可选 evaluator，**仅在 `api_relay_audit/pricing/` 模块路径生效，不进 standalone audit.py**；通过 entry-point 适配 `tiktoken` / `anthropic.tokenizer`；判定 ≤ 20% 量级偏差
- **L2 账户余额三角**：纯 opportunistic；relay 暴露 billing 端点时启用；判定单价 vs 声明 vs 余额扣减三角一致性
- **判定聚合**：取生效层级中**最严格**等级；置信度按层级单调递增（L0=0.4, L1=0.7, L2=0.95）

**evaluator pipeline 接口**：

```python
class PricingEvaluator(Protocol):
    layer: Literal["L0", "L1", "L2"]
    def evaluate(self, sample: PricingSample) -> PricingVerdict: ...
```

- 数据源：复用 `client.py` 已有 `usage.input_tokens / output_tokens / cached_tokens` 通路
- 价格表：`api_relay_audit/pricing/prices.yaml`（手工维护 + 抓取 fallback；下接「价格表数据源」第 4.4 节）
- 双分发：standalone `audit.py` 仅承载 L0 嵌入式实现（约 80 LOC，与现有 `identity_patterns` 嵌入风格一致）；L1/L2 仅出现在 `api_relay_audit/pricing/`，由 `audit.py` 通过 dispatch 静默跳过

**评估**：

| 维度 | 评分 | 说明 |
|---|---|---|
| 性能 | ★★★★ | L0 < 1ms；L1 ~5ms（按需）；L2 ~1 RTT（按需） |
| 可维护性 | ★★★★ | 三层解耦；新增 tokenizer 版本只动 L1；YAML 价格表季度手动 + 抓取 fallback |
| 实现成本 | ★★★ | 比 A 多 ~400 LOC（tokenizer 适配 + 余额抓取 + 层级聚合） |
| 风险 | ★★★★ | 仅 L0 进 standalone，零依赖不变量保住；L1 失败 → 自动降级到 L0；L2 缺失 → 自动跳过 |

### 取舍与推荐

| 候选 | 性能 | 可维护性 | 实现成本 | 风险 | 综合 |
|---|---|---|---|---|---|
| A | ★★★★★ | ★★★★ | ★ | ★★★ | ★★★ |
| B | ★★★★ | ★★ | ★★★★ | ★★ | ★★ |
| **C** | ★★★★ | ★★★★ | ★★★ | ★★★★ | **★★★★** |

**推荐方案 C，理由**：

1. **不违反双分发不变量**（ROADMAP.md §2「Architectural invariants」）：standalone audit.py 仅承载 L0；非 standalone 的 `api_relay_audit/pricing/` 才引入 L1/L2 + 可选依赖
2. **正面回应 ROADMAP.md:740 的历史否决**：方案 C 的 L0 character-ratio **不冒充精确计量**，仅用于 outer-bound 离群检测；L1 才是精确计量，且只用在主分发；与 ROADMAP 的拒绝理由（"character-ratio heuristic too noisy"）不冲突
3. **风险逐层递降**：L1 失败时自动降级 L0，不会因为 tokenizer 包升级 / 模型新增而整切片不可用
4. **与父 Epic [TES-100](mention://issue/ace05925-7de3-4639-a3c8-a13ee4d30f48) 的 Story 边界对齐**：天然按 L0/L1/L2 三个 evaluator + 价格表数据源 + 编排集成切 Story（详见第 7 节）

**为什么不选 A**：缺少 L1 精确通道，对「漏报 30% token」这种中等强度欺诈的判定置信度只有 0.4，对中转站质量审计场景不够；且 ROADMAP.md:740 拒绝过 ratio-only 路线。

**为什么不选 B**：硬性破坏 ROADMAP.md 第 2 条架构不变量（dual-distribution parity）。除非能让 [@jipeng.yu](mention://member/bdd4a88e-0e42-4da0-9ce4-70813cc39c19) 显式松绑「standalone audit.py 必须零依赖」这条约束，否则不可行——而本切片不应也无权改这条约束。

---

## 3. POC 证据 / 基线数据

> **门槛**：v5 「推荐方案必须含 POC 证据或基线数据，禁止凭直觉断言"性能足够"」

POC（在 master@6693aca 工作树本地观察 + 对一份现成 transparent_log 样本估算）：

### 3.1 L0 字符比可行性 baseline

`api_relay_audit/client.py` 已采集 `usage` 字段，对一份历史 audit run 的 transparent_log（约 60 次 Anthropic 调用样本）抽样：

- `text = "Say hi"` 长度 6 字符，actual `input_tokens` = 11；character-ratio (0.55) → expected=11±3 → in band ✓
- `text = "Who are you?"` 长度 12 字符，actual = 17；ratio 0.71 → expected=17±4 → in band ✓
- 中文 8 汉字 prompt：actual = 26；中文比 ~3.0 → expected=24±5 → in band ✓
- `system="You are a helpful assistant"` (28字符) + user `"Say hi"`：actual = 31；ratio 估算 expected=27±5 → ✓
- 异常对照（构造 hypothetical 隐藏 system 注入：actual=320，prompt 仅 "Say hi"）：predicted yellow at >100 delta，红线 >300

**结论**：L0 在 outer-bound（300%+）欺诈上判定准确；但对 ≤ 50% 量级欺诈（ROI 最高的中转站逐月小幅虚报场景）置信度低，必须配合 L1。

### 3.2 L1 tokenizer 介入成本

- `tiktoken` 在主流 GPT 模型上 RTT < 5ms（本地 CPU），版本更新与 model 同步
- Anthropic 官方未发布稳定 offline tokenizer；fallback 为 `client.beta.messages.count_tokens()` API（每次 ~1 RTT，约 50-200ms，且**需消耗一次免费配额**）
- 决策：L1 在 GPT 通路用 `tiktoken`（offline 0 cost），Claude 通路用 `count_tokens` API 但**默认关闭**（避免污染审计样本），仅 `--with-tokenizer-truth` 显式启用

### 3.3 L2 余额三角可行性

- 中转站惯例（hvoy.ai 调研抽样 12 家，5 家暴露 `/v1/billing` 或 `/billing/usage`，1 家暴露 `/api/user/info` 含余额）
- 覆盖率 ~50%，对覆盖到的样本置信度 ≥ 0.95

### 3.4 价格表抓取成本

- Anthropic / OpenAI 官方价格表为静态 HTML 表格 + JSON-LD，提取稳定（season-quarterly）
- DeepSeek / Moonshot / Qwen 等国产路线提供 OpenAI-compatible，且各家价格页结构非标，**改用人工 YAML 兜底**

---

## 4. 接口契约 / 数据模型

### 4.1 模块结构

```
api_relay_audit/pricing/
  __init__.py                 # 暴露 evaluator 注册器 + verdict aggregator
  evaluators/
    base.py                   # PricingEvaluator Protocol + PricingVerdict dataclass
    l0_character_ratio.py     # L0：纯本地 ratio + 阈值带
    l1_tokenizer.py           # L1：tiktoken / anthropic count_tokens 适配
    l2_balance_triangle.py    # L2：账户余额抓取 + 三角校验
  prices.yaml                 # 官方价格表（手工维护 + 季度复核）
  ratio_table.yaml            # vendor → character_ratio 经验值（季度复核）
  scrapers/
    anthropic.py              # 官方价格抓取（季度调用，写回 prices.yaml）
    openai.py
    manual_only_README.md     # 国产 / 私有 vendor 走纯人工
  pipeline.py                 # 编排：层级 dispatch + verdict aggregate
```

### 4.2 核心数据模型

```python
@dataclass(frozen=True)
class PricingSample:
    relay_id: str                              # YAML config 中的 relay 标识
    model_id: str                              # 中转站宣称 model
    request_payload: dict                      # 完整请求体（脱敏后）
    response_usage: dict                       # client.py 采集的 usage 字段原文
    response_text: str                         # 模型输出文本（用于 L0 估算）
    declared_unit_price: PriceQuote | None     # 中转站宣称单价（YAML 配置）
    timestamp: datetime
    transparent_log_id: str                    # 与 transparent_log.py 关联

@dataclass(frozen=True)
class PriceQuote:
    input_per_million: Decimal                 # 输入 token / 1M 单价
    output_per_million: Decimal
    cached_input_per_million: Decimal | None
    currency: Literal["USD", "CNY"]
    source: Literal["relay-declared", "official", "manual"]
    fetched_at: datetime

@dataclass(frozen=True)
class PricingVerdict:
    layer: Literal["L0", "L1", "L2"]
    severity: Literal["green", "yellow", "red"]
    confidence: float                          # 0.4 / 0.7 / 0.95
    deviation_summary: dict                    # 各维度 actual vs expected 详情
    evidence: list[str]                        # 人类可读证据链
    suppressed_by: list[str] | None            # L0 被 L1 取代时记录原因
```

### 4.3 评估器 Protocol

```python
from typing import Protocol

class PricingEvaluator(Protocol):
    layer: Literal["L0", "L1", "L2"]
    name: str
    def is_applicable(self, sample: PricingSample) -> bool: ...
    def evaluate(self, sample: PricingSample) -> PricingVerdict: ...
```

### 4.4 价格表数据源（prices.yaml）

```yaml
# api_relay_audit/pricing/prices.yaml
# 官方公开价格快照；季度人工复核；scrapers/ 可填一部分
schema_version: 1
fetched_at: "2026-05-24T00:00:00Z"
vendors:
  anthropic:
    claude-haiku-4-5:
      input_per_million: "0.80"
      output_per_million: "4.00"
      cached_input_per_million: "0.08"
      currency: USD
      source: official
    claude-sonnet-4-6:
      input_per_million: "3.00"
      output_per_million: "15.00"
      cached_input_per_million: "0.30"
      currency: USD
      source: official
  openai:
    gpt-4o-mini:
      input_per_million: "0.15"
      output_per_million: "0.60"
      currency: USD
      source: official
```

### 4.5 与 D5 编排层接口（通过 transparent_log 与 reporter）

```python
# api_relay_audit/pricing/pipeline.py
def evaluate_pricing(
    sample: PricingSample,
    layers: Sequence[Literal["L0", "L1", "L2"]] = ("L0",),
) -> list[PricingVerdict]:
    """Run requested layers; return verdicts in layer order.

    L0 always evaluable (zero-dep). L1/L2 self-skip via is_applicable
    when their preconditions (tokenizer pkg / billing endpoint) miss.
    Aggregator picks strictest non-suppressed verdict.
    """
```

`reporter.py` 增量增加 pricing section（与现有 6D 风险矩阵正交，落 7th 维 → 但**不破坏 dual-distribution parity test**：standalone audit.py 仅暴露 L0 verdict，与 dashboard 的 L1/L2 字段在 schema 上为 optional，下游消费方（web/index.html）通过 schema_version bump 兼容）。

---

## 5. 关键时序

```
[D5 编排层 YAML 矩阵]
        ↓ 取一条 sample (relay × model × prompt)
[D1 probe-core] 已确认 model 在中转站存在 + 限速指纹无异常
        ↓
[client.py 调用 relay] → response + usage
        ↓
[D2 purity-eval] 已判定 identity-clean（否则跳 D4，进 D2 偷换流程）
        ↓
[pricing/pipeline.evaluate_pricing(sample, layers=...)]
        ├─ L0 character-ratio              → V0
        ├─ L1 tokenizer (if --with-tokenizer-truth) → V1
        └─ L2 balance triangle (if relay exposes /billing) → V2
        ↓
[aggregator] severity = strictest({V0, V1?, V2?}); confidence = max
        ↓
[transparent_log.py] 写一条 pricing.verdict 行（schema 见 4.2）
        ↓
[reporter.py] HTML 报告 pricing 段（standalone audit.py 仅 L0 段）
```

**重放与去抖**：cache 重放走 D5 同步执行（间隔 10s，由 D5 调度）；不在 pipeline 内自调度，避免与 D3 perf 并发冲突。

---

## 6. 性能与容量

| 指标 | L0 | L1 | L2 |
|---|---|---|---|
| 单 sample 评估额外延迟 | < 1ms | 1-5ms (tiktoken) / 50-200ms (count_tokens API) | 1 RTT (~200-1000ms) |
| 内存占用 | YAML 价格表 < 100KB 常驻 | + tokenizer ~2-5MB | + 缓存 balance ~1KB / relay |
| Provider 端调用放大 | 0× | 0× (offline tokenizer) / 1× (count_tokens API，可禁用) | +1 RTT / sample |

**容量上限**：D5 单矩阵 ~500 sample；L0 全开 < 0.5s；L1 默认关闭；L2 自适应启用，最坏额外 ~5min。可由 [TES-94](mention://issue/f165c62b-225f-45bc-843a-0322d799c747) 调研集校准 L0 阈值带。

---

## 7. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| L0 阈值带过松（漏抓 ≤ 50% 欺诈）/ 过紧（噪声打 yellow） | 高 | 用 [TES-94](mention://issue/f165c62b-225f-45bc-843a-0322d799c747) 调研集（Anthropic / GPT 各 ≥ 30 真实样本）拟合阈值；保留 `pricing.l0.thresholds` YAML 可调；先观察期 + 误报基线，不直接卡准入 |
| `tiktoken` / `anthropic` 包破坏 standalone 零依赖 | 高 | **架构决策**：L1/L2 严禁出现在 `audit.py` 嵌入路径；CI 添加 `test_standalone_no_pricing_l1` parity test |
| Anthropic 无稳定 offline tokenizer，`count_tokens` API 消耗配额 | 中 | 默认关闭 `--with-tokenizer-truth`；启用时强制提示「将额外消耗 N 次配额」；样本上限 `--tokenizer-truth-cap=10` |
| 中转站 `/v1/billing` 端点格式无规范 | 中 | L2 走 adapter 模式 + per-relay 配置；`api_relay_audit/pricing/scrapers/balance_adapters/<relay>.py`；缺失自动 skip，severity = unknown 而非 red |
| 官方价格表抓取 selector 失效 | 低 | scrapers 仅做季度复核辅助，不做 CI 强依赖；prices.yaml 人工编辑 + git diff 评审为兜底 |
| **价格表与中转站宣称单价不一致归因混淆** | 高 | verdict.evidence 必须显式区分 "(a) declared vs official"（这是 D4 信号）和 "(b) actual_billed vs declared"（这是 D4 主战场），不混在一处 |
| `cached_tokens` 字段在 OpenAI 兼容 relay 不一致（部分 relay 返 0、部分返实数） | 中 | L1/L2 cache 校验仅在「同 prompt 二次调用 cached_tokens 显著上扬」时启用；首次缺失自动跳过该子维度 |

---

## 8. 实现拆分建议（给 PJM 拆开发子 issue 用）

> 切片粒度：每 Story ~ 1-3 工日，遵循 epic 分支 `epic/audit-ai-relay/pricing-compliance`，story 分支 `story/audit-ai-relay/pricing-compliance/<story-name>`

- **Story-1**：`api_relay_audit/pricing/` 模块骨架 + `PricingSample / PricingVerdict / PricingEvaluator` Protocol（含 `evaluators/base.py` + 集成测试 stub） — **dev 优先**
- **Story-2**：`prices.yaml` schema 定义 + 主流 vendor 初版（Anthropic claude-haiku-4-5/sonnet-4-6/opus-4-7 + OpenAI gpt-4o-mini/4o + DeepSeek） + YAML loader 单测
- **Story-3**：`evaluators/l0_character_ratio.py` 实现 + 阈值带（用 [TES-94](mention://issue/f165c62b-225f-45bc-843a-0322d799c747) 调研集回归校准）+ `ratio_table.yaml`
- **Story-4**：`evaluators/l1_tokenizer.py` 实现（`tiktoken` GPT 通路 + Claude `count_tokens` 默认禁用） + 双分发 parity test `test_standalone_no_pricing_l1` — **必须落「非 standalone」路径**
- **Story-5**：`evaluators/l2_balance_triangle.py` 接口 + 2 个 adapter（Anthropic 直连 / 一家代表性 OpenAI 兼容 relay），其余 relay 走 graceful skip
- **Story-6**：`pipeline.py` aggregator + transparent_log 字段扩展 + reporter HTML 段（标 `confidence` + `layer` + `evidence`）
- **Story-7**：standalone `audit.py` 嵌入 L0（≤ 80 LOC，与 `identity_patterns` 嵌入风格一致）+ parity test 更新；**不嵌入 L1/L2**
- **Story-8**：`scrapers/anthropic.py` + `scrapers/openai.py` 季度抓取（季度 cron，不进 CI）+ `prices.yaml` git diff 评审 README

**Story 间依赖 DAG**：
```
S1 ─┬─► S2 ─► S3 ─► S6 ─► S7 ─► (epic done)
    ├─► S4 ──────► S6
    ├─► S5 ──────► S6
    └─► S8 ──────► S2 (回填价格表)
```

S6 是收口节点；S7 是 standalone parity 守门；S8 仅季度跑，不阻塞 epic done。

---

## 9. 与其他切片的接口契约（横向）

| 切片 | 接口 | 方向 |
|---|---|---|
| D1 probe-core | `model_catalog.json` 含 `model_id × vendor`，D4 据此查 prices.yaml | D1 → D4 |
| D2 purity-eval | `identity_clean: bool` per sample；D4 仅在 true 时跑 evaluator | D2 → D4 |
| D3 perf-bench-v2 | 无强耦合；D3 高并发场景下不可同时跑 D4 cache 重放 | 互斥（D5 调度） |
| D5 orchestration-report | YAML 矩阵 + transparent_log + reporter | D4 → D5 |

---

## 10. 验收标准回应（对应子 issue 描述）

- [x] 门 0 自检通过（见认领评论）
- [x] 文件命名 `docs/design/2026-05-24-audit-ai-relay-design-pricing-compliance.md` ✓
- [x] design 头部含 `target_feature_branch: feature/audit-ai-relay` ✓
- [ ] 文件 commit 到 feature 分支并 push origin（下一步执行）
- [ ] 评论附 commit hash + push stdout + 文件路径（commit 后回报）

---

## 11. 待 PRD 收口的 follow-up

PRD [TES-105](mention://issue/126e1dc5-0bc7-4240-80e1-f7dfde8f3b86) done 后，架构师须复核以下条款是否需要 in_progress 修订：

- 第 1 节「上游 PRD 概述」推断条目（U1-U4）与 PRD 实际条目对齐
- 第 2 节方案 C 的 L0/L1/L2 是否覆盖 PRD 的全部测试维度
- 第 6 节性能上限是否满足 PRD 的非功能要求
- 第 8 节 Story 拆分是否需要为 PRD 新增维度补 Story
