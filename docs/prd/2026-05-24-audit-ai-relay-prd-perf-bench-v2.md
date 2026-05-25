---
issue: [TES-104](mention://issue/64abbb9b-8a99-4fac-b287-5ce2888ae998)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
epic: [TES-99](mention://issue/a9502763-d746-4614-a618-e23ef87e4219)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
slice: perf-bench-v2
status: draft
reviewers: 资深产品经理 / 资深架构师 / 项目管理专家
baseline_commit: 6693aca
基调: 演进式增强（在现有 perf-bench v1 之上扩展，不重写）
---

# PRD 切片 S3 — 性能基准 v2（perf-bench-v2）

## 1. 业务背景

### 1.1 主仓现状（v1 已有能力，作为 v2 起点）

`master@6693aca` 已实现一套可用的性能基准工具，分布在三个位置：

| 模块 | 文件 | 已实现能力 |
|---|---|---|
| 流式客户端 | `api_relay_audit/perf/streaming.py` | `StreamingClient`：OpenAI + Anthropic 双协议 SSE，记录 `ttft` / `total_time` / `text` / `chunk_count` / `finish_reason` / `raw_first_chunk` / `response_headers` |
| 统计 | `api_relay_audit/perf/metrics.py` | `summarize_latencies`：count / min / avg / max / p50 / p90 / p95 / p99 / stdev（线性插值百分位） |
| 编排 | `api_relay_audit/perf/runner.py` | YAML 矩阵：endpoints × models × rounds；vendor preset（gpt / claude）；线程池并发；`/v1/models` 清单获取 |
| CLI | `scripts/perf-bench.py` | 三种调用形态：位置参数 / 单端点 flag / YAML config；HTML + JSON 报告 |
| 配置 | `perf-configs/{example,four-row-vendor,taoken-vs-allall}.yaml` | 已有 3 个示例 |
| 计费层方差 | `api_relay_audit/latency_variance.py` | Step 13：N 次 `max_tokens=8` 探测，CV + bimodality 二分类，作为模型偷换弱信号 |
| 报告 | `api_relay_audit/perf/report.py` | HTML + JSON 渲染（441 行） |
| 测试 | `tests/test_client_stream.py` / `test_latency_variance.py` | 已覆盖流式客户端与方差检测核心路径 |

### 1.2 v2 业务驱动力

中转站审计的用户场景（来自父 issue [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c) 用户原文）涉及"响应时间"维度，但当前 v1 只回答了"端到端有多快"这一题。运营 / 安全 / 选型决策需要更细的子题：

- **TTFT 拆分**：用户体感"卡顿"的根因是首 token 慢还是 token 间慢？v1 只有 TTFT 总值
- **慢启动识别**：中转站冷启 / VRAM 加载导致前 N 个 token 慢，但稳态后追上——v1 没有 chunk 维度时序数据
- **伪流识别**：部分中转站收齐全部 token 后一次性丢出 SSE chunk 模拟流式（用户看似"流式"但等同非流式），v1 无法识别
- **并发曲线**：用户实际使用是并发场景（IDE / 多窗口），单线 p99 不能预测并发 p99，v1 默认 `concurrency=1`，且没有并发梯度扫描
- **稳态/瞬态分布**：v1 把 N 轮 ttft 直接汇总为一个 p99，掩盖了"前 3 轮 + 后 7 轮"两段差异

### 1.3 v2 与 v1 的关系

- **演进基调**：复用 v1 的 `StreamingClient` / `summarize_latencies` / `runner` / CLI / YAML schema；**新增**而不是替换
- **向后兼容**：v1 YAML 配置必须直接可跑（默认值不破坏现状）
- **新功能默认关闭**：并发扫描 / chunk 时序采集 / 慢启动检测均通过 flag 开启，避免冲击现有日常用法
- **复用 latency_variance**：v2 的"伪流识别"借用 bimodality 框架，但维度从"请求间方差"扩展到"chunk 间隔分布"

---

## 2. 用户角色与场景

### 2.1 角色

| 角色 | 关心的问题 | 决策动作 |
|---|---|---|
| **运营 / 选型方**（业务方主诉求） | 这个中转站能不能买？响应快不快、稳不稳、并发顶不顶得住？ | 看 PDF / HTML 报告的 5 张图 + 1 张表，做 GO/NO-GO |
| **安全审计** | 中转站是不是在伪流？是不是冷启时给慢模型、稳态切快模型？ | 看伪流标记 + 慢启动标记，定位证据链 |
| **CI 看门人**（演进 `.github/workflows/`，超出本切片范围） | 这次升级 vs 基线，p99 退化没有？ | 比对 `baseline.json`，超阈值告警 |
| **二次开发者**（dual-distribution 维护） | 我能不能复用 `StreamingClient` 跑自定义对比？ | 直接 import `api_relay_audit.perf` |

### 2.2 场景

- **场景 A — 单端点深度评测**：选型阶段，对一个中转站跑 30 轮 × 3 模型 × 4 并发档位
- **场景 B — 多中转站横向对比**：5 家中转站 × 同一模型 × 同一 prompt 集，输出排序 HTML
- **场景 C — 伪流识别**：用户怀疑某中转站"假流式"，触发 `--detect-pseudo-stream`，输出二分类结论 + 证据 chunk 间隔时序
- **场景 D — 回归基线**：本地跑后产出 `baseline.json`，下次跑用 `--compare-baseline baseline.json` 输出 diff 表

---

## 3. 核心流程

### 3.1 输入

```bash
# v1 兼容形态（必须保持可跑）
python scripts/perf-bench.py <url> <key> <vendor>

# v2 新增：并发扫描
python scripts/perf-bench.py <url> <key> <vendor> \
    --concurrency-sweep 1,2,4,8

# v2 新增：chunk 时序采集（开启后跑伪流 / 慢启动检测）
python scripts/perf-bench.py <url> <key> <vendor> \
    --capture-chunk-timings --detect-pseudo-stream

# v2 新增：基线 diff
python scripts/perf-bench.py --config perf-configs/multi.yaml \
    --compare-baseline reports/baseline.json
```

YAML 扩展（新增字段都是可选）：

```yaml
test:
  prompt: "请介绍北京好吃的"
  rounds: 10
  concurrency: 1
  # v2 新增：
  concurrency_sweep: [1, 2, 4, 8]      # 优先级高于 concurrency
  capture_chunk_timings: false          # 默认关
  detect_pseudo_stream: false
  detect_slow_start: false
  warmup_rounds: 0                      # 不计入统计的预热轮数（避免冷启污染）
```

### 3.2 输出

#### 3.2.1 JSON schema 演进（向后兼容，仅追加字段）

```json
{
  "schema_version": 2,
  "tool": "api-relay-audit perf-bench",
  "generated_at": "2026-05-24T03:00:00Z",
  "endpoints": [{
    "name": "relay-a",
    "results": [{
      "model": "gpt-5.5",
      "metrics": {
        "ttft_seconds":   { "count": 10, "p50": ..., "p95": ..., "p99": ... },
        "total_seconds":  { ... },
        "output_chars":   { ... },
        // v2 新增字段（capture_chunk_timings=true 时填充，否则 null）
        "itl_seconds":          { "count": ..., "p50": ..., "p95": ..., "p99": ... },
        "throughput_chars_s":   { "count": ..., "p50": ..., "p95": ... },
        "first_chunk_ratio":    { "count": ..., "p50": ..., "p99": ... },
        // v2 新增：稳态/瞬态分割
        "warmup_rounds_count":  0,
        "steady_state_metrics": { /* 同 ttft_seconds 结构，剔除前 N 轮 */ }
      },
      // v2 新增：chunk 维度时序（每轮一条数组，capture 关闭时为空）
      "chunk_timings": [
        { "round": 1, "intervals_seconds": [0.02, 0.018, 0.021, ...], "chunk_count": 87 },
        ...
      ],
      // v2 新增：检测器输出（每个检测器独立可关闭）
      "detectors": {
        "pseudo_stream":   { "verdict": "real|pseudo|inconclusive",
                             "evidence": { "chunk_gap_p50": ..., "burst_ratio": ... } },
        "slow_start":      { "verdict": "stable|slow_start|inconclusive",
                             "evidence": { "first_3_chunks_avg": ..., "rest_avg": ... } }
      }
    }],
    // v2 新增：并发扫描结果（仅 concurrency_sweep 存在时填充）
    "concurrency_sweep": [
      { "concurrency": 1,  "ttft_p99": ..., "throughput_chars_s_avg": ..., "success_rate": ... },
      { "concurrency": 2,  "ttft_p99": ..., "throughput_chars_s_avg": ..., "success_rate": ... },
      ...
    ]
  }]
}
```

#### 3.2.2 HTML 报告新增 5 张图 + 1 张总览表

| # | 图 | 数据源 | 用户决策 |
|---|---|---|---|
| F1 | TTFT 分布（端点 × 模型，箱型图） | `metrics.ttft_seconds` | 选型横向对比 |
| F2 | ITL 分布（chunk 间隔） | `metrics.itl_seconds` | 体感卡顿诊断 |
| F3 | 慢启动曲线（轮序 × ttft） | rounds 列表 | 冷启识别 |
| F4 | 并发吞吐曲线（concurrency × throughput） | `concurrency_sweep` | 顶不顶并发 |
| F5 | 伪流证据散点（chunk index × elapsed） | `chunk_timings[].intervals` | 伪流证据链 |
| T1 | 总览表（端点 × 模型 × 6 项核心指标 + 2 项 verdict） | 整体 | GO/NO-GO 一眼判 |

#### 3.2.3 基线 diff（`--compare-baseline`）

输出：

```
endpoint   model           metric       baseline   current    delta     verdict
relay-a    gpt-5.5         ttft_p99     1.42s      1.78s      +25%      regression
relay-a    gpt-5.5         itl_p95      0.024s     0.022s     -8%       improved
relay-a    claude-opus-4-7 success_rate 0.95       0.85       -10pp     regression
```

退化阈值（PRD 给默认值，可在 YAML 覆写）：

| 指标 | 退化阈值 | 改进阈值 |
|---|---|---|
| ttft_p50 | +20% | -10% |
| ttft_p99 | +30% | -15% |
| itl_p95 | +25% | -10% |
| throughput_chars_s avg | -15% | +10% |
| success_rate | -5pp | +5pp |

### 3.3 与 latency_variance（Step 13）的关系

Step 13（`api_relay_audit/latency_variance.py`）当前在 `scripts/audit.py` 内独立调用，**不归入本切片**，但 perf-bench v2 与之有交集：

- Step 13 关心**请求间方差**（同 prompt 跑 N 次 → bimodal 指向模型 A/B 切换）
- perf-bench v2 关心**chunk 间方差**（单请求内 chunk 间隔 → bimodal 指向伪流 / 慢启动）
- 两者复用 `latency_variance.detect_bimodality` 算法（公用函数，不重写）；perf-bench v2 在 `api_relay_audit/perf/` 内调用，不修改 latency_variance 自身

---

## 4. 数据模型骨架

### 4.1 新增类型（dataclass）

```python
# api_relay_audit/perf/streaming.py 内 StreamResult 追加字段
@dataclass
class StreamResult:
    # 已有字段保持不变
    # v2 新增字段，capture_chunk_timings=False 时为 None（避免内存爆炸）
    chunk_intervals: Optional[list[float]] = None    # 相邻 chunk 时间间隔（秒）
    chunk_timestamps: Optional[list[float]] = None   # 各 chunk 到达时刻（perf_counter 相对 start）
```

```python
# api_relay_audit/perf/detectors.py（v2 新增模块）
@dataclass
class PseudoStreamVerdict:
    verdict: str  # "real" | "pseudo" | "inconclusive"
    evidence: dict  # { chunk_gap_p50, chunk_gap_p99, burst_ratio, last_burst_share }

@dataclass
class SlowStartVerdict:
    verdict: str  # "stable" | "slow_start" | "inconclusive"
    evidence: dict  # { first_n_avg, rest_avg, ratio }
```

### 4.2 检测算法骨架（design 切片细化）

PRD 仅定义可观测信号 + 判定方向，具体算法实现归 `architect` 在 design 切片（[TES-109](mention://issue/cebac45b-c708-4121-9dc7-709b81430423)）定。本节列**必须可测的输入/输出契约**：

#### 4.2.1 伪流识别（pseudo-stream detector）

- **输入**：一轮 `chunk_intervals: list[float]`
- **判定方向**（PRD 不锁死阈值，design 切片定）：
  - 真流式：chunk 间隔分布在生成速率附近（典型 0.005–0.05s），最末位 chunk 不应集中包含大部分文本
  - 伪流：极少 chunk（< 3）且每个携带大量文本 / 95% chunk 间隔 < 0.001s 紧接最后一个超大间隔
- **输出**：`verdict ∈ {real, pseudo, inconclusive}`；inconclusive 必须可达（如 `chunk_count < 3` 或全部 error）
- **置信度边界**：单轮判定不足以定 verdict——按多轮 vote（多数派 + 50% 占比阈值），不足 3 轮成功 → inconclusive

#### 4.2.2 慢启动（slow-start detector）

- **输入**：一组多轮 `ttft_seconds: list[float]`，按轮序排列
- **判定方向**：前 N 轮（N=`warmup_rounds` or 默认 3）avg vs 剩余 avg 的比值
- **输出**：`verdict ∈ {stable, slow_start, inconclusive}`；ratio > 1.3 → slow_start（design 切片定具体阈值）
- **副作用**：可选地把 `warmup_rounds` 从主统计剔除，单独算 `steady_state_metrics`，让 p99 不被冷启拉偏

#### 4.2.3 并发扫描（concurrency sweep）

- **输入**：`concurrency_sweep: list[int]`，对每个值跑 rounds × 模型一次
- **输出**：每档 `{ concurrency, ttft_p50, ttft_p99, throughput_chars_s_avg, success_rate }`
- **预算保护**：sweep 长度 > 8 → 报错（避免一行 YAML 把账户烧穿）；总请求数 = `sum(sweep) * rounds * models * endpoints`，超 200 → 警告（design 阶段确定 cap）

### 4.3 与 dual-distribution invariant 的关系

**根仓 `audit.py` 不背 perf-bench v2**。理由：

- `audit.py` 是 13 步审计的 curl-only 单文件，性能基准与审计走两条独立通路（`scripts/perf-bench.py` vs `scripts/audit.py`），共享 0 行业务逻辑
- 父仓 `CLAUDE.md` Dual Distribution Model 节明确：dual 维护的是 audit 通路的字符级一致性，不波及 perf
- v2 不修改 `latency_variance.py` 本身（Step 13 是 audit 通路的一部分，dual 已覆盖），只在 `api_relay_audit/perf/` 内调用其纯函数

**强制约束（验收 P0）**：S3 改动**只允许触及** `api_relay_audit/perf/`、`scripts/perf-bench.py`、`perf-configs/`、`tests/test_perf_*` 与 `docs/`；不得改 `audit.py` 根仓副本、`api_relay_audit/{client,context,error_leakage,identity_patterns,infra_fingerprint,latency_variance,stream_integrity,tool_substitution,transparent_log}.py`。如 design 阶段发现必须改其他模块，必须回炉本 PRD 走 PJM 共识，不得越界。

---

## 5. 合规与约束

### 5.1 父仓 CLAUDE.md 约束（必须满足）

- **可编辑无需问**：`scripts/perf-bench.py` ✅、`api_relay_audit/perf/` ✅、`tests/` ✅、`perf-configs/` ✅、`docs/prd/` ✅
- **需问后改**：`web/`、`.github/workflows/`、`docs/`（已有内容）、`deploy/`、根级 config——**S3 不进**
- **doc-drift prevention**：本 PRD 不引用任何数字版本号 / step count / test count，避免触发 `collect-metrics.py` 校验
- **dual-distribution invariant**：v2 完全不动 `audit.py` 根仓副本（见 4.3）

### 5.2 vendor-feedback-driven，非投机性增量

每个 v2 新功能必须挂一个 v1 实际暴露的用户痛点：

| v2 功能 | v1 用户场景痛点 |
|---|---|
| chunk_timings 采集 | 用户报告"看似流式实则一次性" → 缺证据链 |
| pseudo-stream detector | 同上，自动化判定避免人工分析 chunk 间隔 |
| slow-start detector | 多轮 p99 被前几轮拉偏，跑得越多越准的现象不直观 |
| concurrency sweep | 选型方反馈"单线快但并发烂"，v1 默认 c=1 无法识别 |
| baseline diff | CI 场景反馈"看 HTML 报告对比很累"，需结构化 diff |

### 5.3 安全 / 隐私 / 凭证

- **凭证不落 JSON / HTML 报告**：现有 v1 已合规（仅记录端点名 + 脱敏 host）；v2 继承
- **chunk_timings 包含原始文本**：v2 报告**默认不导出全文 chunk_text**，仅导出时间戳 + 字符长度（避免敏感 prompt 回流；`text_preview` 已限 280 字符的策略保留）
- **真实 key 入 YAML 的风险**：保持 v1 现状（`perf-configs/example.yaml` 已用占位符示例 + README 提醒环境变量优先）；本切片不重构凭证存储（归 S5 `orchestration-report` 子能力 ④ 凭证安全）

### 5.4 预算保护

- `concurrency_sweep` 长度上限 8（design 切片定）
- `rounds × len(concurrency_sweep) × len(models) × len(endpoints)` 总请求估算 > 500 → 命令前置 confirm 提示（design 切片定）
- `--dry-run` flag：仅打印将发送请求总数与预估时长，不真发请求

### 5.5 与外网依赖的约束

- v2 不引入新依赖（继续只用 `httpx` + 可选 `pyyaml`），与父仓 `pip install httpx pytest` 一致
- 不引入绘图库；HTML 图表用纯 SVG / Canvas + 嵌入式 JS（保留 v1 现状方案，无需额外 pip 包）

---

## 6. 验收标准（可测）

### 6.1 P0 — 必须达成（门 5 dev → QA 闸门）

- [ ] **v1 兼容性**：现有三种 CLI 形态（位置参数 / 单端点 flag / YAML config）全部能跑通 v1 现有 `perf-configs/example.yaml`，输出 schema 1 JSON 字段完整无丢失
- [ ] **JSON schema v2**：`schema_version` 字段变为 `2`；v1 字段一字不改；新增字段在 capture 关闭时为 `null` 或缺省
- [ ] **chunk_timings 采集**：`--capture-chunk-timings` 开启后，单轮请求 JSON 含 `chunk_intervals` 且长度 = `chunk_count - 1`；关闭时 `chunk_timings` 为 `[]`
- [ ] **pseudo-stream detector**：构造 mock 流式服务器（`tests/` 内）模拟两类响应：
  - 真流（每 chunk 间隔 ~0.02s，10+ chunks）→ verdict = `real`
  - 伪流（2 chunks，最后一个超大）→ verdict = `pseudo`
  - 错误响应（HTTP 500）→ verdict = `inconclusive`
- [ ] **slow-start detector**：构造 mock 让前 3 轮 TTFT = 2s、后 7 轮 TTFT = 0.5s → verdict = `slow_start`；全部 0.5s → verdict = `stable`
- [ ] **concurrency_sweep**：`--concurrency-sweep 1,2,4` 跑通后 JSON 含 3 档结果；每档 `ttft_p99` 是数值
- [ ] **baseline diff**：跑两次生成两份 JSON，第二次用 `--compare-baseline old.json` 输出 stdout 含 5 列表 + 退化项标记
- [ ] **HTML 报告**：5 张图（F1–F5）全部渲染，可在浏览器打开（不依赖 CDN），1 张总览表（T1）数值匹配 JSON
- [ ] **dual-distribution 不动**：`tests/test_dual_distribution_parity.py` 全绿（v2 commit 后跑一次）
- [ ] **doc-drift 守门**：`python scripts/collect-metrics.py` 跑过后 `docs/_metrics.md` 不再现 schema diff（即 v2 不引入会触发 collect-metrics 的可量化"step / test / version"宣称）

### 6.2 P1 — 建议达成

- [ ] **预算保护**：`--dry-run` 输出总请求数与预估耗时；`concurrency_sweep > 8` 报错退出
- [ ] **warmup_rounds**：YAML 配置 `warmup_rounds: 3` 后，主统计 `ttft_seconds.p99` 不含前 3 轮；`steady_state_metrics` 字段填充
- [ ] **chunk 隐私**：报告 HTML 不出现原始 prompt / response 全文（仅 280 字符 preview，与 v1 一致）

### 6.3 P2 — 可选（v2.1 再议）

- [ ] **多 prompt 矩阵 chunk_timings**：当前 v2 仅采集单 prompt 的 chunk 时序；多 prompt 的 chunk 时序对比留 v2.1
- [ ] **chunk_text 完整导出选项**（`--export-full-text`）：用户主动开启 + 自动 mask 敏感字段；v2 不实现，需走 S5 凭证安全协同

---

## 7. 风险与未决问题

### 7.1 风险

| 风险 | 影响 | 缓解 |
|---|---|---|
| **chunk_intervals 内存爆炸**：长输出 + 多轮 + 多模型 → 数百 MB JSON | 报告打不开 / OOM | 默认关；开启后单轮 chunk 数超 10000 自动截断保留 head/tail（design 切片定阈值） |
| **伪流检测误报**：低带宽网络下真流也可能呈现"大间隔 + 突发" | 错杀正常中转站 | verdict 含 `inconclusive` 兜底；HTML 报告同步展示 chunk 间隔散点（F5），让用户自己看证据 |
| **并发扫描烧 token**：sweep × rounds × models × endpoints 量级爆炸 | 账户成本不可控 | 预算保护 + `--dry-run`；YAML 加 `max_total_requests` 硬上限 |
| **httpx 线程池在高并发下不稳**：`concurrent.futures.ThreadPoolExecutor` 在 c > 16 时性能不可预测 | 高并发档位数据失真 | sweep 上限 8；design 切片决定是否要切 `asyncio + httpx.AsyncClient` |
| **冷启识别误判 vendor TPS 限流**：429 反复重试也会前 N 轮慢 | slow_start 误报 | detector 输入只取 `ok=True` 的轮次；error 单独看 |
| **新人贡献者破坏 dual invariant**：误以为 perf-bench v2 要同步进 `audit.py` | tests fail 拉锯 | PRD 4.3 节 + design 切片需要写明边界，CI 已有 parity test 守底线 |

### 7.2 未决问题（等 design / 实施阶段拍板）

| # | 问题 | 决策方 | 不阻塞本 PRD |
|---|---|---|---|
| Q1 | pseudo-stream / slow-start 的具体阈值 | 架构师 design 切片 [TES-109](mention://issue/cebac45b-c708-4121-9dc7-709b81430423) | ✅ 不阻塞 |
| Q2 | concurrency_sweep 是否改 asyncio | 架构师 design 切片 | ✅ 不阻塞 |
| Q3 | F1–F5 五张图的具体 SVG / Canvas 实现方案 | 架构师 design 切片 | ✅ 不阻塞 |
| Q4 | baseline.json 的存储位置（git 跟踪 / artifact / 本地） | S5 orchestration-report PRD（[TES-106](mention://issue/3419145a-f8fa-4a1c-91d9-919b65518b13)） | ✅ 不阻塞，本 PRD 只定 schema |
| Q5 | CI 集成 `--compare-baseline` 触发条件 | 超出本切片，归 S5 / `.github/workflows/` 范围 | ✅ 不阻塞 |

### 7.3 与其他切片的边界（避免越界）

| 关联切片 | 重叠点 | 边界明示 |
|---|---|---|
| S1 probe-core | `/v1/models` 清单获取 | **v2 不重写 `fetch_models`**；继续在 `streaming.py` 内调用 v1 实现；如 S1 重构 `fetch_models` 上抽，v2 跟随但不抢工 |
| S2 purity-eval | `analyze_purity` 已在 `runner.py` 内调用 | **保持不动**，v2 改动不触碰 purity 检测器；新增 `chunk_timings` 字段不影响 purity 输入（purity 用 `r.text`） |
| S4 pricing-compliance | `output_chars` / `throughput_chars_s` 可作为价格合规的隐含输入 | **不耦合**；v2 输出 throughput，是否用于价格 diff 归 S4 设计 |
| S5 orchestration-report | baseline 存储 / CI 集成 / HTML 报告聚合 | **本 PRD 只定 baseline JSON schema 与 diff stdout 格式**；存储 / CI / 多切片聚合大盘归 S5 |
| Step 13 latency_variance | bimodality 算法 | **复用 `detect_bimodality` 公用函数**，不修改 latency_variance；v2 在 perf 模块内引入 |

---

## 8. 切片签收清单（PRD → design 转交）

提交本 PRD in_review 时附下列证据：

- [x] PRD 头部含 `target_feature_branch: feature/audit-ai-relay`
- [x] 已读父 issue [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c) 全部 34 条评论
- [x] 已读父仓 `CLAUDE.md`，并据此约束 4.3 / 5.1 节
- [x] 已勘查 master@6693aca 现状：`scripts/perf-bench.py`、`api_relay_audit/perf/{streaming,metrics,runner,purity,report}.py`、`api_relay_audit/latency_variance.py`
- [x] 验收标准 P0 全部可测（mock server / parity test / dry-run / JSON 字段断言）
- [x] 与 S1/S2/S4/S5/Step 13 边界明示，未越界
- [x] 风险表覆盖 6 项，每项有缓解
- [x] 5 个未决问题全部下推给 design 切片或下游 PRD，不阻塞本 PRD in_review

---

## 9. 变更日志

| 日期 | 版本 | 作者 | 变更 |
|---|---|---|---|
| 2026-05-24 | draft v0 | 资深产品经理 | 初稿，按 v5 流程产出 |
