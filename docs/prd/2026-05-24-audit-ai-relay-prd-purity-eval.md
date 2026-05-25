---
issue: [TES-103](mention://issue/48c40d3b-79c0-4d03-b6b8-7d61ba8094d1)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
epic_issue: [TES-98](mention://issue/05a725a5-6513-4667-98b6-8d60fbbfa228)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
slice: purity-eval
status: draft
reviewers: 资深产品经理 / 资深架构师 / 项目管理专家
baseline: master@6693aca
tone: 演进式增强（非重写）
date: 2026-05-24
---

# PRD 切片 S2：纯净度评估（purity-eval）

## 0. TL;DR

把 master@6693aca 已存在但散落的 6 个纯净度检测能力，整合成**统一可调度的 evaluator pipeline**，对一次中转站请求产出可比对的「纯净度六维结论」。六维 = **注水 / 偷换 / 身份漂移 / 工具替换 / refusal / 错误泄漏**。

本切片不引入真源对照（twin call），不改 perf 路径产出形态，不动 detectors 内部规则；只做"骨架对外的统一形态、入口、报告契约、阈值化判定"。

---

## 1. 业务背景

### 1.1 上游驱动

父 issue [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c) 要求构建"AI 中转站质量审计"项目。调研子 issue [TES-94](mention://issue/f165c62b-225f-45bc-843a-0322d799c747) 经一轮 PM × 架构师对齐，把审计范围切成 5 个需求族（S1–S5），其中 **S2 = 纯净度评估**，针对 LLM 中转站最高频、最易被察觉、用户底线损失最大的 6 类作弊行为。

行业实证数据（CISPA "Real Money, Fake Models" 2026-03 论文）显示，在 400 家被测自由路由器中：

| 作弊类型 | 检出率 | 对应本切片维度 |
|---|---|---|
| 模型替换（cheap-model swap） | **45.83%** | 偷换 / 身份漂移 |
| Credential / error 泄漏 | 4.25% | 错误泄漏 |
| Tool-call rewriting | 2.00% | 工具替换 |
| Prompt 注入 / system leak | 行业普查无量化但可观 | 注水 |
| 越权 refusal / 静默截断 | 与策略强关联 | refusal |

→ 这 6 维不是"feature list"，是**用户付费换 token 的最低安全底线**。任一维出问题，付费即被欺诈。

### 1.2 主仓现状（master@6693aca）

主仓已有的零件（PM 已亲手 grep 验证，文件路径精确）：

| 文件 | 已覆盖维度 | 当前形态 | 缺什么 |
|---|---|---|---|
| `api_relay_audit/perf/purity.py` | 注水（system_prompt_leak / relay_internal_token） + 身份漂移（identity_leak 带 anchored） + refusal + language_mismatch + empty | `analyze_response()` per-round + `analyze_purity()` 聚合 + `_verdict()` 4 档（clean / suspicious / injected / failed） | **绑死在 perf-bench 流式响应语料上**；无独立入口；只产生整体 verdict，没有六维分项报告 |
| `api_relay_audit/identity_patterns.py` | 偷换关键词库（GPT/GLM/DeepSeek/Qwen 等品牌词 + CJK） | `find_non_claude_identities(text)` 纯文本扫描 | 仅作为 purity 内部依赖被调用 |
| `api_relay_audit/tool_substitution.py` | 工具替换（pip / npm / cargo / go 安装命令字符级回放） | `run_tool_substitution_test(client)` 4 个 probe + classify(expected,received) | **独立 13-step 审计的一步**，未进入 purity 聚合视图 |
| `api_relay_audit/error_leakage.py` | 错误泄漏（6 个 break-me 探针，扫 key 前缀 / Authorization / 上游 URL / 路径 / 堆栈） | `run_error_leakage_test(client, api_key, base_url, aggressive)` 产出 hits[] + 最高严重度 | **独立 13-step 审计的一步**，未进入 purity 聚合视图 |
| `api_relay_audit/web3/injection_probes.py` | 注水的 web3 变体（钱包地址 / 私钥诱导） | `run_web3_injection_probes(client)` 产出 `Web3InjectionResult` | 独立步骤；与通用注水检测器并列 |
| 测试覆盖 | `tests/test_*.py` 已覆盖 identity / error_leakage / tool_substitution / refusal_detector / web3_injection | 单元测试格式 | 没有"六维联跑"的集成用例 |

**整合负担一句话**：6 个能力已落地，但**调用方式 5 种、结果数据结构 5 种、verdict 口径 2 套**，无法在一份报告里横向对照。

### 1.3 切片定位（演进，非重写）

> v5 基调原文："演进式增强（master@6693aca 已有子系统骨架）"

本切片**不重写 6 个 detector 中的任何一个**，**不改 perf 路径已有的同语料复用机制**。范围限定为：

- ✅ 引入 `EvaluatorPipeline`（新增轻量编排层）+ `PurityReportV2`（新增聚合 schema）
- ✅ 让现有 6 个 detector 成为可独立调用的 Evaluator 实现（适配，不内移逻辑）
- ✅ 统一一份 CLI / SDK 入口，能跑全六维、能跑单维、能跑 detectors 子集
- ✅ 阈值化判定 → 给每维独立 verdict（clean / suspicious / injected / failed / unknown）
- ✅ 报告（JSON + HTML 段落）契约固化，与 perf 报告可以并列
- ❌ 不引入真源对照（twin call）— 留给后续 `audit-engine` design 切片 + B2 真源 key 解锁后
- ❌ 不动 detectors 内部规则集 / 关键词库 / 阈值（只暴露阈值常量，不调整）
- ❌ 不替换 13-step 标准审计入口（继续向后兼容）

---

## 2. 用户角色与场景

### 2.1 用户角色

| 角色 | 关心维度 | 用得到的入口形态 |
|---|---|---|
| **A. 中转站采购方（业务方）** | 这家 relay 能不能用、有没有偷换 / 注水 | 一行 CLI 出一张 HTML 报告，红绿分明 |
| **B. relay 运维 / 售后（被测方）** | 找出自家中转链路上的偷换 / 泄漏环节 | 单维 CLI 复现 + 详细 JSON 定位到 round_index |
| **C. 测试工程师（项目内部）** | 把六维作为回归基线挂在 CI 上 | Python SDK 入口 + JSON schema 稳定可 diff |
| **D. 安全研究员** | 收集行业作弊样本、对照基线 | 单 detector 入口 + 完整原始响应留底 |

### 2.2 关键场景（按角色 × 用例）

**场景 1：业务方决策（A）**
> "我刚买了 X 家的 relay，跑一遍 GPT/Claude 两个 vendor 全套体检"

```bash
python scripts/purity-eval.py https://relay.example.com sk-XXX gpt --rounds 10 \
    --output reports/relay-gpt-purity.html
```

期望：
- 单文件 HTML，浏览器双击即看
- 六维各自显示 verdict + 关键证据（如「identity_leak: 3/10 轮检出 'DeepSeek'」、「error_leakage: severity=high, kind=auth_header_echo」）
- 总体 overall verdict 给出"建议是否使用"的红绿判定
- JSON sidecar 同名输出便于二次分析

**场景 2：售后定位（B）**
> "客户报 system prompt 漏了，我要单测注水维度并看到具体响应文本"

```bash
python scripts/purity-eval.py https://relay.example.com sk-XXX gpt \
    --only injection --rounds 30 --raw-keep --output reports/inject-only.json
```

期望：
- `--only injection`（或 identity / refusal / tool / error / web3）只跑一维，节省 token
- `--raw-keep` 保留每轮完整响应文本（默认只存 400 字符预览），便于复盘
- 报告里每个 hit 必须带 `round_index` + 原始证据 snippet

**场景 3：CI 回归（C）**
> "把 purity-eval 作为合并门挂在 nightly，任意一维 verdict=injected 就 fail"

```python
from api_relay_audit.evaluator import EvaluatorPipeline, default_purity_pipeline

pipeline = default_purity_pipeline()
report = pipeline.run(client=client, profile="quick")  # 跑得快的快速集
assert report.overall.verdict == "clean", report.summary()
```

期望：
- SDK 入口存在，`PurityReportV2` 可 pickle/json 化
- `profile="quick"` 对应一个 ≤2min 的快测集；`profile="full"` 是完整六维
- JSON schema 标注版本号（`schema_version: "purity-v2.0"`），便于跨版本 diff

**场景 4：研究采样（D）**
> "我想专门扫一遍 100 家公开 relay 的偷换情况，按月度出报告"

期望：通过 SDK 把六维拆开按需调用，原始响应可结构化导出（不在本切片必交付，但**接口设计要为这种用法留出口**）。

### 2.3 非目标场景（明确排除）

| 场景 | 为什么不在本切片 |
|---|---|
| 真源对照（先打官方 API，再打 relay，对二元组下结论） | B2 阻塞未解锁；架构师在 `audit-engine` design 切片承担（[2c20e7bc](mention://comment/2c20e7bc)） |
| Web UI（人工挑战式 prompt 输入 + 在线复盘） | B3 阻塞未解锁；S1 `cli-and-ui` 切片决定 |
| 性能指标（TTFT / p95 / p99） | 属于 S3 `perf-bench-v2`，purity 复用 perf 语料但不重复产 perf 指标 |
| /v1/models 清单 diff | 属于 S1 `probe-core` |
| 价格 / 计费精度 | 属于 S4 `pricing-compliance` |

---

## 3. 核心流程

### 3.1 端到端调用链

```
用户 (CLI / SDK)
    │
    │  (1) 选定 target_url + api_key + vendor + profile
    ▼
EvaluatorPipeline.run()                                 ← 新增编排层
    │
    │  (2) 构造 ProbeContext（client / prompt 集合 / 配置）
    ▼
按 profile 选 Evaluators 子集
    │  default profile 至少含 6 个：
    │    InjectionEvaluator   ← 适配 purity.analyze_response 的 system_leak / relay_token
    │    SwapEvaluator        ← 适配 identity_leak + anchored 校验
    │    DriftEvaluator       ← 适配 identity_leak 的另一切面（多轮一致性 P1，本切片只做单轮）
    │    ToolSubstEvaluator   ← 适配 run_tool_substitution_test
    │    RefusalEvaluator     ← 适配 purity 的 REFUSAL_PATTERNS
    │    LeakEvaluator        ← 适配 run_error_leakage_test
    │
    │  (3) 各 Evaluator 独立执行（顺序，不并发，避免共享 client 状态污染；并发是 P1）
    ▼
每个 Evaluator 产出 EvaluatorResult
    {
      dimension: "injection" | "swap" | "drift" | "tool" | "refusal" | "leak",
      verdict: "clean" | "suspicious" | "injected" | "failed" | "unknown",
      hits: [ ... 维度专属证据 ... ],
      rounds_total: int,
      rounds_ok: int,
      verdict_reason: str,
      raw_artifacts: optional[list[dict]]    # --raw-keep 时填充
    }
    │
    ▼
PurityAggregator.aggregate(results) → PurityReportV2
    │
    │  - 计算 overall.verdict（按"最差子维度"规则，injected > suspicious > failed > clean > unknown）
    │  - 计算 overall.confidence（按 ok 轮占比加权）
    │  - 附 metadata（target_url、vendor、profile、schema_version、started_at、duration_s）
    ▼
Reporter
    │  - write_json_report() → reports/<name>.json
    │  - write_html_report() → reports/<name>.html（六维段落布局）
    ▼
退出码：
    overall.verdict in {clean}           → exit 0
    overall.verdict in {suspicious,unknown} → exit 0（默认） / exit 2（--strict）
    overall.verdict in {injected,failed} → exit 1
```

### 3.2 Evaluator 适配现状对照（演进而非重写）

| 维度 | Evaluator | 适配的现有函数 | 适配负担 |
|---|---|---|---|
| 注水（injection） | `InjectionEvaluator` | `api_relay_audit/perf/purity.py::analyze_response()` 的 system_leak / relay_token 分支 | 抽取分支为独立函数；包装出 hits[] 形态 |
| 偷换（swap） | `SwapEvaluator` | `identity_patterns.find_non_claude_identities()` + `purity._identity_anchored()` | 同上，单独跑一遍 anchored 校验，不依赖 perf 语料 |
| 身份漂移（drift） | `DriftEvaluator` | 与 swap 共享底层；本切片单轮判定 = swap 子集；多轮一致性进 P1 | 标 P1，本切片 MVP 仅给"占位 verdict + 引用 swap.hits"，**不引入新检测逻辑** |
| 工具替换（tool） | `ToolSubstEvaluator` | `tool_substitution.run_tool_substitution_test(client)` | 直接调用 + 把 4 个 probe 结果汇总为 hits[] |
| refusal | `RefusalEvaluator` | `purity.REFUSAL_PATTERNS` + `analyze_response()` refusal 分支 | 与 InjectionEvaluator 复用文本扫描 |
| 错误泄漏（leak） | `LeakEvaluator` | `error_leakage.run_error_leakage_test(client, api_key, base_url, aggressive)` | 直接调用 + 转 hits[] |

> **Web3 注入**：业务上属于注水的子族。本切片**默认不在 default profile**（避免在非 web3 场景产生噪音 hits），但暴露 `--include web3` 开关挂载 `Web3InjectionEvaluator`（适配 `run_web3_injection_probes`）。

### 3.3 与 perf-bench 的关系（复用而非耦合）

| 关系 | 说明 |
|---|---|
| **复用语料的能力保留** | 现有 perf-bench 跑完后直接喂 `PurityRecord` 给 `analyze_purity()` 的路径**不动**；HTML 报告仍出 v1 verdict |
| **独立入口新增** | `scripts/purity-eval.py` 是新入口，**自带流量生成**（默认问"请介绍北京好吃的"基线 prompt），不依赖 perf-bench 跑过 |
| **数据结构兼容** | `EvaluatorPipeline` 产出的 `PurityReportV2` 是新结构；`PurityRecord` / `PuritySummary` v1 不删 |
| **互操作 P1** | 让 perf-bench 的 HTML 报告可选嵌入 `PurityReportV2`（替换或附加 v1 verdict），统一报告口径 — **不在本切片必交付**，但 schema 设计上要为此留位 |

---

## 4. 数据模型骨架

### 4.1 输入：`ProbeContext`

```python
@dataclass
class ProbeContext:
    """统一传给所有 Evaluator 的上下文，复用 master 已有的 APIClient 抽象。"""
    client: APIClient                # 复用 api_relay_audit.client.APIClient（master 已有）
    api_key: str                     # 给 leak evaluator 做 key-redact 用
    base_url: str                    # 给 leak / drift evaluator 用
    vendor: str                      # "gpt" | "claude"，决定 prompts / 期望身份关键词
    rounds: int = 10                 # 默认 10 轮（与 perf-bench 默认对齐）
    prompts: list[str] = field(default_factory=list)  # 空 = 用各维度默认 prompt 集
    sleep_between: float = 1.0       # 调用间隔
    keep_raw: bool = False           # --raw-keep
```

### 4.2 单维输出：`EvaluatorResult`

```python
@dataclass
class EvaluatorResult:
    dimension: str                   # 见 4.4 维度枚举
    verdict: str                     # clean | suspicious | injected | failed | unknown
    verdict_reason: str              # 一行可读结论，必填
    rounds_total: int
    rounds_ok: int
    hits: list[dict]                 # 维度专属证据（见 4.5 hits schema）
    duration_s: float
    raw_artifacts: list[dict] | None = None  # keep_raw=True 时填，否则 None
```

### 4.3 总报告：`PurityReportV2`

```python
@dataclass
class PurityReportV2:
    schema_version: str = "purity-v2.0"
    metadata: dict                   # target_url / vendor / profile / started_at(RFC3339) / duration_s
    dimensions: dict[str, EvaluatorResult]   # key 见 4.4
    overall: Overall

@dataclass
class Overall:
    verdict: str                     # 同 EvaluatorResult.verdict 五档
    verdict_reason: str
    confidence: float                # 0.0–1.0，基于 sum(rounds_ok)/sum(rounds_total) 加权
    worst_dimensions: list[str]      # 决定 overall 的维度名
```

### 4.4 维度枚举（schema 稳定 ID）

| 维度 ID | 显示名 | 默认 profile 是否在内 |
|---|---|---|
| `injection` | 提示词注水 | ✅ |
| `swap` | 模型偷换 | ✅ |
| `drift` | 身份漂移 | ✅（MVP 仅占位） |
| `tool` | 工具替换 | ✅ |
| `refusal` | refusal | ✅ |
| `leak` | 错误泄漏 | ✅ |
| `web3_injection` | web3 注水变体 | ❌（`--include web3` 显式打开） |

### 4.5 `hits[]` schema 约定（每维 hits 至少含的字段）

```jsonc
{
  "severity": "high" | "medium" | "low",
  "kind": "<维度专属 kind 字符串，如 identity_leak / system_prompt_leak / typosquat / auth_header_echo>",
  "round_index": 0,                  // 对应第几轮请求；error_leakage 用 probe_id
  "snippet": "...证据片段，<= 200 chars，已 redact key...",
  "where": "response_body" | "response_header" | "tool_call_echo" | "...",
  "details": { /* 维度专属补充 */ }
}
```

### 4.6 Verdict 升级规则（overall）

```
overall.verdict =
    "injected" if any dim.verdict == "injected"
    else "suspicious" if any dim.verdict == "suspicious"
    else "failed" if any dim.verdict == "failed"
    else "unknown" if any dim.verdict == "unknown"
    else "clean"
```

> **不引入加权打分**：行业实证表明任一维 injected = 用户付费即被欺诈，不可被"其他维度 clean"稀释。confidence 是观察可靠度，不参与 verdict 升档。

---

## 5. 合规与约束

### 5.1 数据安全

| 约束 | 落地点 |
|---|---|
| API key 不进报告 | `leak` 维度沿用 `error_leakage._redact_api_key()`；其他维度的 `hits.snippet` 通过 `EvaluatorResult` 统一过一道 redact 函数 |
| 原始响应默认不落盘 | `keep_raw=False` 时 `raw_artifacts=None`；只在 `--raw-keep` 时保留，并在 HTML 报告头部用红字提示 |
| 不上送任何第三方 | 报告均落本地文件，不引入 telemetry |

### 5.2 行业 / 法务

- ❌ 不调用任何官方 API 做对照（B2 未解锁，避免在未授权下烧用户 token）
- ❌ 不主动尝试越权 / 绕过 relay 的速率限制（保持探针文明：rounds ≤ 50、sleep ≥ 0.5s）
- ❌ 不复制 hvoy.ai / relayAPI 等同类项目的代码（继续独立重新实现，沿用主仓 `identity_patterns.py` 已有的归因声明）

### 5.3 兼容性

- `api_relay_audit/perf/purity.py` 的 v1 `analyze_response` / `analyze_purity` / `_verdict` 公共函数**不删、不改签名**
- 13-step audit 入口（独立调用 `run_tool_substitution_test` / `run_error_leakage_test` 等）**保持向后兼容**
- 新增模块挂在 `api_relay_audit/evaluator/`（架构师可在 design 切片中调整位置，但 PRD 层面只约束**不破坏现有 import**）

### 5.4 阻塞与依赖

| 阻塞 | 状态 | 对本切片影响 |
|---|---|---|
| **B1 仓库同步** | ✅ 已解锁（[15f6d292](mention://comment/15f6d292)，5 epic 分支已 push） | 本 PRD 可正常 commit |
| **B2 真源 key** | 🔴 未解锁 | 不引入 twin call；架构师 design 切片承担 |
| **B3 Web UI 范围** | 🔴 未解锁 | 本切片只交付 CLI + SDK，不交付 UI；HTML 报告维持单文件双击模式 |

---

## 6. 验收标准（可测）

每条 AC 都写明"如何验证"。

### 6.1 功能 AC（必交付）

- [ ] **AC-F1**：`scripts/purity-eval.py <url> <key> <vendor>` 单路最简形式可跑通，10 轮默认 prompt，产出 HTML + JSON
  - 验证：`python scripts/purity-eval.py https://example.com sk-fake gpt --rounds 2 --output /tmp/p.html`（fake key 也应 graceful 跑完，所有维度 verdict=`failed`，不抛未捕获异常）
- [ ] **AC-F2**：JSON 报告含 6 个维度块 + 1 个 overall，`schema_version="purity-v2.0"`
  - 验证：`jq '.schema_version, .dimensions | keys, .overall.verdict' /tmp/p.json` 命中
- [ ] **AC-F3**：`--only <dim>` 支持单维执行（dim ∈ injection/swap/drift/tool/refusal/leak/web3_injection）
  - 验证：`--only tool` 时 JSON 只含 1 个 dimensions 键
- [ ] **AC-F4**：`--include web3` 把 web3_injection 加进 default profile
  - 验证：JSON dimensions 含 7 个键
- [ ] **AC-F5**：`--raw-keep` 把每轮原始响应留底；不开此开关时 `raw_artifacts` 为 null
- [ ] **AC-F6**：HTML 报告六维各有独立段落，每段含 verdict / verdict_reason / hits 表 / rounds_ok 进度
  - 验证：人工 diff master perf-bench HTML 段落布局，本切片新增 6 段
- [ ] **AC-F7**：SDK 入口 `from api_relay_audit.evaluator import EvaluatorPipeline` 可导入并跑通
  - 验证：`pytest tests/test_evaluator_pipeline.py`（架构师 design 切片需求一项）

### 6.2 整合 AC（演进而非重写）

- [ ] **AC-I1**：v1 `purity.analyze_response()` / `analyze_purity()` / `_verdict()` 签名与行为不变
  - 验证：`tests/test_clean_summary_flags.py` 现有用例不修改即通过
- [ ] **AC-I2**：13-step 标准审计调用路径（`run_tool_substitution_test` / `run_error_leakage_test`）不破坏
  - 验证：现有 `tests/test_tool_substitution.py` / `tests/test_error_leakage.py` 不修改即通过
- [ ] **AC-I3**：6 维 Evaluator 实现**复用**现有检测函数，不复制规则常量（`SYSTEM_LEAK_PATTERNS` / `REFUSAL_PATTERNS` / `LEAKAGE_TOKENS` / `_IDENTITY_ANCHORS` 仅在原文件存在一份）
  - 验证：`rg -l "SYSTEM_LEAK_PATTERNS\s*=\s*\[" api_relay_audit/` 只命中 `perf/purity.py` 一份

### 6.3 报告契约 AC

- [ ] **AC-R1**：overall verdict 升档规则严格按 §4.6 实现
  - 验证：构造 mock results（如 5 维 clean + 1 维 injected），断言 overall = injected
- [ ] **AC-R2**：所有 `hits[].snippet` 不含 api_key 完整字符串或前 8 字符
  - 验证：`grep -F "$(echo $API_KEY | head -c 8)" report.json` 必须 0 命中
- [ ] **AC-R3**：JSON 报告可以重复跑两次后 diff（除 metadata 时间戳外字段稳定）
  - 验证：`jq 'del(.metadata.started_at, .metadata.duration_s)' a.json > a.norm; jq 'del(...)' b.json > b.norm; diff a.norm b.norm` 0 行差异

### 6.4 退出码 AC

- [ ] **AC-X1**：overall=clean → exit 0
- [ ] **AC-X2**：overall=injected / failed → exit 1
- [ ] **AC-X3**：overall=suspicious / unknown → 默认 exit 0；加 `--strict` 后 exit 2

### 6.5 非功能 AC

- [ ] **AC-N1**：default profile 全六维（不含 web3）10 轮在本地 mock client 下 ≤ 30s 完成
- [ ] **AC-N2**：报告 HTML 单文件 ≤ 500KB（含内联 CSS，不含外部依赖）
- [ ] **AC-N3**：报告 JSON 在 `--raw-keep=false` 时 ≤ 50KB

### 6.6 文档 AC

- [ ] **AC-D1**：`README.md` 新增"纯净度六维评估"小节，列出 CLI 三种用法 + 维度速查表
- [ ] **AC-D2**：`docs/` 下补一份 `purity-v2-schema.md` 描述 JSON schema（架构师 design 切片产出，PM 只在此 PRD 引用其文件名）

---

## 7. 风险与未决问题

### 7.1 已识别风险

| # | 风险 | 严重度 | 触发条件 | 缓解 |
|---|---|---|---|---|
| R1 | "身份漂移" 与"模型偷换"在 MVP 同源（都基于 identity_patterns），可能让 drift 维度看起来"白送" | 中 | 用户疑问"为啥两维都基于同一份关键词" | PRD §3.2 明确 drift 在 MVP 是 swap 子集 + verdict_reason 注明"placeholder，多轮一致性进 P1"；P1 时引入"问 10 次 'who made you'，是否每次答案一致"独立检测 |
| R2 | 默认 prompt "请介绍北京好吃的" 在某些 relay 上会触发 refusal 噪音 | 低 | 部分内容审查策略激进的 relay | 提供 `--prompt-file` 让用户换 prompt；默认 prompts 集设计成"低争议 + 高 token 覆盖" |
| R3 | tool_substitution 的 4 个 probe 对中性 relay 也可能误报（模型自行解释命令而非原样回显） | 中 | 模型在低温下仍可能改写 | `tool_substitution.classify()` 现有的 `_strip_wrappers()` 已处理常见 markdown 噪音；如果命中率异常，调阈值进 P1 |
| R4 | 没有真源对照时，"偷换"维度依赖关键词命中，对"沉默模型"（不自报家门的偷换）失效 | 高 | relay 用 system prompt 强制模型不自报身份 | PRD 显式声明这是 MVP 局限；解锁 B2 后由 `audit-engine` design 切片引入 LLMPrint 指纹检测覆盖 |
| R5 | 多个 Evaluator 顺序执行，慢 | 低 | full profile 60+ 轮 | 本切片不做并发；P1 由架构师 design 切片在 `audit-engine` 引入调度器 |

### 7.2 未决问题（PM × 架构师 × PJM 需对齐）

- **Q1（→ 架构师）**：`api_relay_audit/evaluator/` 这个模块位置合不合适？是否要叫 `api_relay_audit/purity/`？是否要把 `perf/purity.py` 顺道挪进去？**PM 提议保持 `evaluator/`**（其他切片如 perf-bench-v2 / pricing-compliance 也会复用 Evaluator 抽象），但最终位置由 design 切片定夺
- **Q2（→ 架构师）**：`EvaluatorPipeline` 是否需要支持并发（asyncio）？本 PRD 标 P1，design 切片如认为顺序版工程量太低不值得拆，可一次性写并发版
- **Q3（→ PJM）**：本 PRD 的 P1 项（drift 多轮一致性 / 并发执行 / perf 报告嵌入 v2）拆到哪个后续 Story 子 issue？是否单开 Story？PM 倾向"统一收口为 1 个 P1 Story"
- **Q4（→ 项目负责人）**：B2 真源 key 预算何时锁定？锁定后是否在 S2 Epic 后再开"twin-call enhancement" Story 把 swap 维度升级（不影响本 PRD MVP）

### 7.3 不在本切片解决（明确转单）

| 项目 | 转交对象 |
|---|---|
| 真源对照机制（twin call orchestrator） | 架构师 `audit-engine` design 切片 |
| LLMPrint 指纹检测 | 架构师 `detectors-fingerprint` design 切片 + 后续 Story |
| Web UI / 报告聚合可视化 | B3 解锁后 PJM 决定是否单开 |
| 多模态 / Embedding / TLS / 计费精度 | S7 advanced detectors 切片 |
| /v1/models diff / endpoint 可达性 | S1 `probe-core` 切片 |

---

## 8. 演进点 vs 主仓现状速查

> 给架构师做 design 切片时一眼看清「改什么 / 不改什么」

| 主仓资产 | 演进动作 | 不动 |
|---|---|---|
| `api_relay_audit/perf/purity.py` | 抽 `analyze_response()` 内的 system_leak / refusal 子逻辑为可独立调用的纯函数 | `PurityRecord` / `PuritySummary` 字段；`SYSTEM_LEAK_PATTERNS` / `REFUSAL_PATTERNS` / `LEAKAGE_TOKENS` 常量 |
| `api_relay_audit/identity_patterns.py` | 无 | `find_non_claude_identities()` 签名与关键词库 |
| `api_relay_audit/tool_substitution.py` | 在 `ToolSubstEvaluator` 内包装一次 | `run_tool_substitution_test()` / `PROBES` 常量 / `classify()` |
| `api_relay_audit/error_leakage.py` | 在 `LeakEvaluator` 内包装一次 | `run_error_leakage_test()` / 探针集合 / redact 函数 |
| `api_relay_audit/web3/injection_probes.py` | 在 `Web3InjectionEvaluator` 内包装；默认不挂 default profile | `Web3InjectionResult` / `classify_web3_response()` |
| `scripts/perf-bench.py` | 无（不动） | 全文件 |
| `tests/test_*.py` 现有 5 个 | 不修改 | 全文件 |
| 新增 | `api_relay_audit/evaluator/` 模块、`scripts/purity-eval.py` 入口、`tests/test_evaluator_pipeline.py`（位置由 design 切片定） | — |

---

## 9. 交付清单

> design 切片接到本 PRD 后，应产出能落地下列产物的方案

1. `api_relay_audit/evaluator/__init__.py` — 暴露 `EvaluatorPipeline` / `default_purity_pipeline` / `PurityReportV2` 公共 API
2. `api_relay_audit/evaluator/base.py` — `Evaluator` 抽象基类 + `ProbeContext` + `EvaluatorResult` dataclass
3. `api_relay_audit/evaluator/dimensions/*.py` — 6 个 Evaluator 实现（适配现有函数）
4. `api_relay_audit/evaluator/aggregator.py` — `PurityAggregator` + verdict 升档逻辑
5. `api_relay_audit/evaluator/reporter.py` — JSON + HTML 报告生成
6. `scripts/purity-eval.py` — CLI 入口
7. `tests/test_evaluator_pipeline.py` + `tests/test_evaluator_aggregator.py` — 整合测试 + verdict 升档单测
8. `README.md` 章节更新
9. `docs/purity-v2-schema.md` — JSON schema 文档（design 切片产出，PM 已在 §6.6 AC-D2 引用）

---

## 10. 版本与签收

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v0.1 draft | 2026-05-24 | 首版按 v5 规约产出 | 资深产品经理 |

**签收节奏（PJM 协调）**：

1. 本 PRD commit 到 `feature/audit-ai-relay` → PM 在 [TES-103](mention://issue/48c40d3b-79c0-4d03-b6b8-7d61ba8094d1) 评论附 commit hash + push stdout
2. 资深架构师 review（重点：§3、§4、§7.2 Q1/Q2）→ 通过即解锁 [TES-108](mention://issue/97315f64-cfd2-48dd-8668-a4acd916d1a2) S2 design 切片
3. 项目管理专家 review（重点：§6 AC 可测性、§7.2 Q3 P1 拆分）→ 通过即在 [TES-98](mention://issue/05a725a5-6513-4667-98b6-8d60fbbfa228) Epic 下拆 Story
4. 三方 review 任一返修 → status 回 draft；PM 在新 commit 中标注 v0.x
