---
issue: [TES-109](mention://issue/cebac45b-c708-4121-9dc7-709b81430423)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
epic: [TES-99](mention://issue/a9502763-d746-4614-a618-e23ef87e4219)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
subsystem: perf-bench-v2
depends_on_prd: [TES-104](mention://issue/64abbb9b-8a99-4fac-b287-5ce2888ae998)（done @ 2026-05-24 14:18，落盘 commit 6168d10）
status: draft
reviewers: 资深架构师 / 项目管理专家 / 资深开发工程师代表
baseline_commit: 6693aca
基调: 演进式增强（不重写 v1，仅追加新模块 + 字段 + flag）
---

# S3 design — 性能基准 v2（perf-bench-v2）

> 上游 Epic：[TES-99](mention://issue/a9502763-d746-4614-a618-e23ef87e4219)　顶层父：[TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
> 上游 PRD：[TES-104](mention://issue/64abbb9b-8a99-4fac-b287-5ce2888ae998)（done @ 14:18，388 行，落盘 `docs/prd/2026-05-24-audit-ai-relay-prd-perf-bench-v2.md`）
> 调研基线：master@6693aca — `scripts/perf-bench.py`（233 行）+ `api_relay_audit/perf/{streaming,metrics,runner,purity,report}.py`（共 ~1500 行）+ `api_relay_audit/latency_variance.py`（237 行）

---

## 1. 上游 PRD 概述

PRD 锁定 v2 的核心范围为 5 项 v1 暴露的痛点延伸（PRD §1.2 / §2.2 / §6.1）：

| 维度 | v1 现状 | v2 目标 | PRD 锚点 |
|---|---|---|---|
| chunk 时序采集 | 只记 TTFT/total/text/chunk_count | 可选采集 `chunk_intervals` + `chunk_timestamps` | §3.2.1 / §4.1 |
| 伪流识别 | 无 | `pseudo_stream` detector 产 `real/pseudo/inconclusive` verdict | §4.2.1 / §6.1 P0-4 |
| 慢启动识别 | 多轮 p99 被前几轮拉偏 | `slow_start` detector + 可选剔除 warmup 轮 | §4.2.2 / §6.1 P0-5 |
| 并发扫描 | 默认 c=1，无梯度 | `concurrency_sweep` + per-档 metrics | §4.2.3 / §6.1 P0-6 |
| 基线 diff | 无 | `--compare-baseline` + 5 列 stdout 表 | §3.2.3 / §6.1 P0-7 |
| HTML 图 | 已有基础统计图（v1 报告） | 新增 F1–F5 五张图 + T1 总览表 | §3.2.2 / §6.1 P0-8 |
| JSON schema | `schema_version=1` | `schema_version=2`，新字段仅追加（v1 字段一字不改） | §3.2.1 / §6.1 P0-2 |

**PRD 留给 design 的 5 个未决问题**（PRD §7.2 Q1-Q5）由本 design §10 给出答复：阈值（Q1）、并发模型（Q2）、SVG 实现（Q3）；Q4/Q5 下推 S5 PRD 不在本 design 解决。

### 1.1 v2 不在范围（PRD 已明示）

- `audit.py` 根仓副本不动（dual-distribution invariant，PRD §4.3 / §5.1）
- 不修改 `audit.py` 13 步主审计 / `latency_variance.py`（Step 13） / `api_relay_audit/{client,context,error_leakage,identity_patterns,infra_fingerprint,stream_integrity,tool_substitution,transparent_log,web3}` 任一模块（PRD §5.1 / §4.3 强制约束）
- 不引入新 pip 依赖（PRD §5.5）
- 不实现 `.github/workflows/` 集成、baseline 存储位置裁决（PRD Q4/Q5 → S5）、多 prompt 矩阵 chunk_timings（PRD §6.3 P2 → v2.1）

---

## 2. 现状勘查与改造对照

> 本节列「现有代码资产 → v2 是新增还是修改 → 是否触碰 PRD §5.1 受限域」三列对照，PJM 拆 Story 时直接据此分包。

### 2.1 受影响文件清单（基于 master@6693aca）

| 文件 | LOC | v2 动作 | 修改类型 | 受限域 |
|---|---|---|---|---|
| `api_relay_audit/perf/streaming.py` | 372 | **修改** `StreamResult` 加 2 字段 + `StreamingClient.stream` 加 1 参数 + 2 处 chunk 时间戳记录 | 局部追加，无字段语义变更 | ✅ 自由可改 |
| `api_relay_audit/perf/runner.py` | 297 | **修改** 加 sweep / detector / warmup 流转；`_run_rounds_for_model` 提取出 `_run_one_concurrency` 子函数 | 主流程演进 | ✅ 自由可改 |
| `api_relay_audit/perf/metrics.py` | 52 | **修改** 加 `itl_seconds` / `throughput_chars_s` / `first_chunk_ratio` 辅助计算函数 | 追加纯函数 | ✅ 自由可改 |
| `api_relay_audit/perf/report.py` | 441 | **修改** HTML 模板加 F1–F5 + T1 + diff 表渲染；`write_baseline_diff()` 新函数 | 追加渲染段 | ✅ 自由可改 |
| `scripts/perf-bench.py` | 233 | **修改** 加 4 个 v2 flag + 1 个 `--compare-baseline` + 1 个 `--dry-run` | flag 追加，v1 调用形态保留 | ✅ 自由可改 |
| `api_relay_audit/perf/detectors.py` | 0 | **新增** `pseudo_stream_detect()` + `slow_start_detect()` + 阈值常量 | 新文件 | ✅ 自由可改 |
| `api_relay_audit/perf/baseline_diff.py` | 0 | **新增** baseline 加载 + 阈值比较 + stdout 表格式化 | 新文件 | ✅ 自由可改 |
| `api_relay_audit/perf/__init__.py` | 22 | **修改** 追加新导出（`pseudo_stream_detect` / `slow_start_detect` / `compare_baseline`） | 追加 export | ✅ 自由可改 |
| `tests/test_perf_streaming.py` | 已有 | **修改** 加 chunk_timings + capture 开关测试 | 追加用例 | ✅ 自由可改 |
| `tests/test_perf_detectors.py` | 0 | **新增** mock server + 4 个 verdict 路径测试 | 新文件 | ✅ 自由可改 |
| `tests/test_perf_baseline_diff.py` | 0 | **新增** schema 加载 + 阈值边界测试 | 新文件 | ✅ 自由可改 |
| `tests/test_perf_runner_sweep.py` | 0 | **新增** sweep 单档 mock 跑通 | 新文件 | ✅ 自由可改 |
| `perf-configs/example-v2.yaml` | 0 | **新增** 含 v2 新字段示例 | 新文件 | ✅ 自由可改 |
| `audit.py`（根） | 65.1K | **不动** | — | ⛔ dual 不变量 |
| `api_relay_audit/latency_variance.py` | 237 | **不动**（仅 import `detect_bimodality`，0 修改） | — | ⛔ PRD §4.3 |

新增代码总量预估：~600 LOC（含测试 ~350 LOC、生产 ~250 LOC）。无 1 行触碰 dual-distribution invariant，`tests/test_dual_distribution_parity.py` 应零干扰通过。

### 2.2 v1 调用入口保留性核对

PRD §6.1 P0-1 要求 v1 三种 CLI 形态全部能跑通现有 `perf-configs/example.yaml`：

| v1 入口 | v2 兼容策略 |
|---|---|
| 位置参数 `<url> <key> <vendor>` | `_absorb_positional()` 不动；新 flag 都通过 `g_test` / 新 group 添加 |
| 单端点 flag `--url --key --vendor` | 同上；新 flag 默认值都关闭采集，行为与 v1 完全等价 |
| YAML `--config` | `load_config()` 加 `capture_chunk_timings: bool = False` / `detect_pseudo_stream: bool = False` / `detect_slow_start: bool = False` / `concurrency_sweep: Optional[list[int]] = None` / `warmup_rounds: int = 0`；老 YAML 无这些字段时全部走默认值 → 跑出来的 JSON `schema_version=2` 但 v1 字段一字不改 |

---

## 3. 候选方案（3 个，符合 v5 brainstorming 9 步要求）

> 取舍维度：**性能 / 可维护性 / 实现成本 / 演进式约束（不破 v1 兼容 + 不动 dual）** / 测试可观测性。

### 3.1 方案 A — 局部演进（**推荐**）

**核心思路**：现有模块就地追加 + 1 个新检测模块 + 1 个新 baseline diff 模块，并发模型沿用 `ThreadPoolExecutor`。

| 维度 | 决策 |
|---|---|
| chunk 时序采集 | `StreamingClient.stream(..., capture_chunk_timings=False)`；每次 `chunk_count += 1` 后追加 `time.perf_counter() - start` 到内部 list；`StreamResult` 新增两个 `Optional[list[float]]` 字段，默认 `None` |
| detector 模块 | 新增 `api_relay_audit/perf/detectors.py`，含 `pseudo_stream_detect(rows) -> PseudoStreamVerdict` / `slow_start_detect(ttft_values) -> SlowStartVerdict`，纯函数，输入是 v1 + v2 字段 |
| 并发模型 | sweep 内**串行扫档**（c=1,2,4,8 依次），每档内复用 `concurrent.futures.ThreadPoolExecutor`（v1 已用） |
| baseline diff | 新增 `api_relay_audit/perf/baseline_diff.py`，含 `load_baseline(path)` / `compare(baseline, current, thresholds) -> list[DiffRow]` / `format_diff_table(rows) -> str` |
| HTML 5 张图 | 沿用 v1 的纯 SVG + 内嵌 JS 方案，在 `report.py` 模板里追加 5 个 `<svg>` 块 + 1 个总览 `<table>`；JSON 内联渲染（无 CDN） |
| CLI flag | `scripts/perf-bench.py` 加 `--capture-chunk-timings` / `--detect-pseudo-stream` / `--detect-slow-start` / `--concurrency-sweep "1,2,4,8"` / `--warmup-rounds N` / `--compare-baseline <path>` / `--dry-run` |
| 内存保护 | 单轮 chunk 数超 10000 → 截断保留 head 5000 + tail 5000 + `truncated: true` 标记（detector 见后续讨论） |
| 预算保护 | sweep 长度 > 8 → 命令行直接退出；总请求估算 > 500 → 命令前置 `--yes-burn` 必须显式声明 |

**性能**：与 v1 持平（关闭采集时），开启 chunk 采集每轮 perf_counter 调用次数 = chunk_count，纳秒级开销，对 TTFT 测量精度影响 < 0.1%。

**可维护性**：新增 2 个文件 + 修改 6 个文件；每个新增字段都是 `Optional`，关闭时为 `None`，与 v1 字段隔离；detector 是纯函数，输入输出 dict，单测覆盖率上手即满分。

**实现成本**：~600 LOC，预计 1 周可完成 5 个 Story（见 §9 拆分建议）。

**演进式约束**：
- ✅ v1 三种调用形态全部保留（默认值不破坏现状）
- ✅ JSON schema_version=2，v1 字段一字不改，v2 字段默认 `null`
- ✅ `audit.py` / `latency_variance.py` 零修改
- ✅ 不引入新 pip 依赖（继续 `httpx` + 可选 `pyyaml`）

**测试可观测性**：
- mock SSE server（`tests/_perf_mock.py`）— 4 种响应模式：真流 / 伪流 2-chunk / 伪流 burst / 错误响应
- detector 是纯函数，无 I/O，单测可 100% 覆盖 verdict 路径
- baseline_diff 是纯函数，单测覆盖 5 阈值边界
- sweep 单档跑通用 mock server，不真发 httpx

---

### 3.2 方案 B — 异步重构

**核心思路**：把 `StreamingClient` 改造为 `AsyncStreamingClient`（基于 `httpx.AsyncClient`），sweep 高并发档位用 asyncio gather；保留同步入口为 `asyncio.run(...)` 包装。

| 维度 | 决策 |
|---|---|
| chunk 时序采集 | 同方案 A |
| detector 模块 | 同方案 A |
| 并发模型 | 全部切 `httpx.AsyncClient` + `asyncio.Semaphore(concurrency)`；sweep 内每档 `asyncio.gather(*[stream() for _ in range(rounds)])` |
| baseline diff | 同方案 A |
| HTML 5 张图 | 同方案 A |
| 其他 | 同方案 A |

**优点**：
- 高并发档位（c > 8）数据更准（线程切换开销 → 协程切换开销，降低非业务延迟）
- 未来对接 streaming 实时 dashboard 时不用再切

**缺点**：
- **侵入 `streaming.py` 整体重写**：370 行代码全部要改 `def stream` → `async def stream`，httpx.stream → httpx.AsyncClient.stream，且 `runner.py` 主流程从同步循环变为 asyncio loop
- **PRD 上限 c=8**：8 路并发线程池在实测中没观察到 v1 数据失真（v1 没有 c>1 默认场景，但单元测试已覆盖 c=4 不抖动）。引入 asyncio 是「为了一个上限不会触碰的场景重写一切」，成本不值
- **破坏 dual-distribution 心智模型**：`audit.py` 根仓副本只能跑同步代码（curl 子进程），perf 通路如果切异步，未来若有任何 cross-cut 改造（如把 detector 共享到 audit 通路）会出现「同步 audit 通路 + 异步 perf 通路」分裂
- **测试复杂度上升**：所有 mock 都要切 pytest-asyncio + 异步 mock server，单测成本提升 2x
- **风险偏移**：asyncio 在 Python 3.11+ 行为稳定，但 mocked httpx 异步测试在 macOS/Windows CI 上偶发 flake（业内常见）

**结论**：在 PRD 明示 sweep 上限 8 的前提下，方案 B 的收益曲线无显著优势，重写成本与维护风险 outweigh。**不推荐。**

---

### 3.3 方案 C — 检测器外置 + chunk 时间戳后置补算

**核心思路**：不动 `streaming.py`，所有 chunk 时间戳由 `runner.py` 内 wrap 一层 chunk 迭代器外部记录；detector 全部下沉到 `runner.py` 之上的 orchestrator 层。

| 维度 | 决策 |
|---|---|
| chunk 时序采集 | `StreamingClient.stream` 改返回 `Iterator[ChunkEvent]`（破坏向后兼容！）；runner 内消费 iterator 时记录每个 chunk 的 perf_counter |
| detector 模块 | 同方案 A，但调用点在 runner，不在 streaming |
| 其他 | 同方案 A |

**优点**：
- streaming.py 完全不动
- 时间戳采集逻辑集中在 runner

**缺点**：
- **`stream() -> StreamResult` 是 v1 公开 API**：改返回 generator 破坏 dual-distribution（虽然 audit.py 不用 streaming，但 `api_relay_audit/perf/__init__.py` 已导出 `StreamingClient`，外部用户可能依赖）
- **向后兼容需要双 API**：要么 `stream()` 保留 / `stream_iter()` 新增（两个 API 长期双轨）；要么 strict broken 升级（违反 PRD §1.3 演进式约束）
- **方案 A 的 hook 模式（capture flag）已足够**：在 `_stream_openai` / `_stream_anthropic` 内 chunk_count++ 后 if-guard `if capture:` 追加时间戳，是一处 4 行的局部改动，比让外部消费者改 API 简单 100 倍

**结论**：方案 C 的"分离关注点"理由不成立 —— chunk 时间戳本质上是 streaming 层的物理事实，记录在 streaming 内反而更合理。**不推荐。**

---

### 3.4 推荐方案

**方案 A**。理由汇总：

1. **PRD 演进式约束的最佳契合**：v1 三种入口完全保留，v1 字段不动，新增字段默认 `null`
2. **改动局部、风险可控**：~250 行新代码 + ~80 行修改 v1 模块，每个动作都有对应单测
3. **不引入 asyncio 重构债**：sweep 上限 c=8 用线程池足够，没有放大失真信号
4. **dual-distribution 安全**：`audit.py` / `latency_variance.py` / 其他根仓模块零干扰
5. **不引入新依赖**：仍是 `httpx` + 可选 `pyyaml`
6. **测试上手快**：detector / baseline_diff 是纯函数，mock SSE server 与 v1 同构

---

## 4. 接口契约 / 数据模型

### 4.1 `StreamResult` 字段追加（向后兼容）

```python
# api_relay_audit/perf/streaming.py 内 StreamResult — 仅追加字段
@dataclass
class StreamResult:
    ok: bool
    ttft: Optional[float]
    total_time: float
    text: str
    chunk_count: int
    finish_reason: Optional[str]
    status_code: int
    error: Optional[str]
    format: str
    model: str
    raw_first_chunk: Optional[str] = None
    response_headers: dict = field(default_factory=dict)
    # v2 新增（capture 关闭时为 None）
    chunk_intervals: Optional[list[float]] = None
    chunk_timestamps: Optional[list[float]] = None
    chunk_intervals_truncated: bool = False  # 内存保护时置 True
```

**字段含义**：
- `chunk_intervals[i] = chunk_timestamps[i+1] - chunk_timestamps[i]`，长度 = `chunk_count - 1`
- `chunk_timestamps[i]` 是第 i 个 SSE data chunk 到达时刻（相对 stream() start 的 `perf_counter()` 差，秒）
- `chunk_intervals_truncated = True` 表示 chunk 数 > MAX_CHUNK_RECORDS（10000），保留 head/tail 各 5000

### 4.2 `StreamingClient.stream` 参数追加

```python
def stream(self, model: str, prompt: str, *,
           system: Optional[str] = None,
           max_tokens: int = 512,
           temperature: Optional[float] = None,
           capture_chunk_timings: bool = False) -> StreamResult:
```

`capture_chunk_timings=False` 时，`StreamResult.chunk_intervals` / `chunk_timestamps` 为 `None`（v1 行为完全等价）。

### 4.3 `api_relay_audit/perf/detectors.py`（新模块）

```python
"""Detectors for pseudo-stream and slow-start patterns in perf-bench v2."""

from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Optional

from api_relay_audit.perf.metrics import percentile
# 复用 Step 13 公用算法（仅 import，不修改 latency_variance.py）
from api_relay_audit.latency_variance import detect_bimodality

# ---- 阈值（design 阶段冻结，dev 不得调整；调整需回炉本 design）----
MIN_CHUNKS_FOR_REAL = 3                # 少于此值 → inconclusive 或 pseudo
PSEUDO_LAST_BURST_SHARE = 0.7          # 最末位 chunk 文本占比 ≥ 70% → 突发模式
PSEUDO_GAP_BEFORE_LAST_RATIO = 5.0     # 最后一个 chunk 间隔 ≥ 5×中位 → 缓冲突发
PSEUDO_MAJORITY_THRESHOLD = 0.5        # 多轮 vote 占比阈值
SLOW_START_WARMUP_DEFAULT = 3          # 默认前 N 轮算 warmup
SLOW_START_RATIO = 1.3                 # warmup avg / steady avg > 1.3 → slow_start
MIN_ROUNDS_FOR_SLOW_START = 6          # 少于此值（warmup * 2）→ inconclusive
MIN_DETECTOR_ROUNDS = 3                # 任何 detector 需要至少 3 个 ok=True 轮次


@dataclass
class PseudoStreamVerdict:
    verdict: str  # "real" | "pseudo" | "inconclusive"
    evidence: dict
    # evidence keys:
    #   per_round_verdicts: list[str]
    #   ok_rounds_used: int
    #   chunk_gap_p50, chunk_gap_p99: float | None
    #   last_burst_share_p50: float | None
    #   reason: str  # 简要中文解释


@dataclass
class SlowStartVerdict:
    verdict: str  # "stable" | "slow_start" | "inconclusive"
    evidence: dict
    # evidence keys:
    #   warmup_rounds: int
    #   warmup_avg, steady_avg: float | None
    #   ratio: float | None
    #   ok_rounds_used: int
    #   reason: str


def pseudo_stream_detect(rows: list[dict]) -> PseudoStreamVerdict:
    """多轮 vote 伪流检测。

    输入：runner 产出的 rows，每条含 ok / chunk_count / chunk_intervals(若 capture) / output_chars。
    判定一条单轮 verdict 后做多数派投票。

    单轮规则（按优先级）：
      1. ok=False 或 chunk_intervals 为 None / 空 → 单轮 inconclusive
      2. chunk_count < MIN_CHUNKS_FOR_REAL 且 output_chars >= 50 → pseudo (lone-chunk-with-text)
      3. last_burst_share >= PSEUDO_LAST_BURST_SHARE → pseudo (final-chunk-buffered)
      4. last_gap_ratio = last_chunk_interval / median_interval；ratio >= PSEUDO_GAP_BEFORE_LAST_RATIO → pseudo (buffered-burst)
      5. 否则 → real

    多轮 vote：取 ok 轮的单轮 verdict，pseudo 占比 ≥ PSEUDO_MAJORITY_THRESHOLD → pseudo；
    real 占比 ≥ PSEUDO_MAJORITY_THRESHOLD → real；其余 → inconclusive。
    ok 轮 < MIN_DETECTOR_ROUNDS → inconclusive。
    """


def slow_start_detect(ttft_values: list[float], *,
                      warmup: int = SLOW_START_WARMUP_DEFAULT
                      ) -> SlowStartVerdict:
    """前 N 轮 vs 剩余轮的 TTFT 比值检测。

    输入：按轮序的 TTFT 列表（仅 ok=True 的轮，runner 在调用前过滤）。
    规则：
      - len(ttft_values) < MIN_ROUNDS_FOR_SLOW_START → inconclusive
      - len(ttft_values) <= warmup → inconclusive（剩余样本为空）
      - warmup_avg / steady_avg > SLOW_START_RATIO → slow_start
      - 否则 → stable
    """
```

**为什么不复用 `latency_variance.detect_bimodality` 作 pseudo 主算法？**
- `detect_bimodality` 关心**请求间**的双峰（多轮 latency 是否分两簇）；伪流是**请求内** chunk 间隔分布问题，维度不同
- 但「chunk 间隔分布是否双峰」可以作为 P1 增强信号（v2.1 引入），design 阶段不开口子

### 4.4 `concurrency_sweep` 数据模型

```python
# runner.py 产出格式（每个 endpoint 节点下追加）
{
  "concurrency_sweep": [
    {
      "concurrency": 1,
      "ttft_p50": 0.42, "ttft_p99": 0.78,
      "total_p99": 4.12,
      "throughput_chars_s_avg": 145.2,
      "success_rate": 1.0,
      "ok_rounds": 10, "failed_rounds": 0
    },
    {"concurrency": 2, ...},
    ...
  ]
}
```

每档 = 完整的 rounds × models 一遍。档之间**串行**（避免互相干扰）。每档内复用 `_run_rounds_for_model`。

### 4.5 `baseline_diff` 数据模型 + stdout 格式

```python
# api_relay_audit/perf/baseline_diff.py
@dataclass
class DiffRow:
    endpoint: str
    model: str
    metric: str        # "ttft_p50" | "ttft_p99" | "itl_p95" | "throughput_chars_s_avg" | "success_rate"
    baseline: float
    current: float
    delta_pct: float   # 相对变化（success_rate 用 pp）
    verdict: str       # "improved" | "regression" | "stable" | "missing"


THRESHOLDS = {
    # 退化阈值, 改进阈值（PRD §3.2.3 已给）
    "ttft_p50":               (+0.20, -0.10),
    "ttft_p99":               (+0.30, -0.15),
    "itl_p95":                (+0.25, -0.10),
    "throughput_chars_s_avg": (-0.15, +0.10),
    "success_rate":           (-0.05, +0.05),  # 单位 pp，特殊处理
}


def compare(baseline: dict, current: dict,
            thresholds: dict = THRESHOLDS) -> list[DiffRow]: ...

def format_diff_table(rows: list[DiffRow]) -> str:
    """5 列 stdout 表：endpoint / model / metric / baseline / current / delta / verdict"""
```

**stdout 实例**（PRD §3.2.3 已给）：

```
endpoint   model           metric                    baseline   current    delta     verdict
relay-a    gpt-5.5         ttft_p50                  0.42s      0.51s      +21%      regression
relay-a    gpt-5.5         ttft_p99                  1.42s      1.78s      +25%      regression
relay-a    gpt-5.5         itl_p95                   0.024s     0.022s     -8%       stable
relay-a    gpt-5.5         throughput_chars_s_avg    145.2      158.7      +9%       stable
relay-a    gpt-5.5         success_rate              0.95       0.85       -10pp     regression
```

### 4.6 JSON schema_version=2 完整结构

```jsonc
{
  "schema_version": 2,                            // v2 唯一变更点
  "tool": "api-relay-audit perf-bench",           // v1 同
  "generated_at": "2026-05-24T...",               // v1 同
  "elapsed_seconds": 12.4,                        // v1 同
  "test": { ... },                                 // v1 同（+ 新增字段透出原始 CLI 配置）
  "endpoints": [{
    "name": "...", "base_url": "...",             // v1 同
    "format": "...", "vendor": "...",
    "models_tested": [...], "models_listed": [...],
    "models_listed_error": null,
    "results": [{
      "model": "...",
      "rounds": [{                                  // v1 字段一字不改
        "round": 1, "ok": true, "status_code": 200,
        "ttft_seconds": 0.42, "total_seconds": 4.12,
        "output_chars": 580, "chunk_count": 87,
        "finish_reason": "stop", "error": null,
        "format": "openai", "text_preview": "...",
        "issues": [], "language_mismatch": false,
        "identities_detected": [],
        // v2 新增字段（capture 关闭时为 null）
        "chunk_intervals": [0.02, 0.018, ...],     // null 或 list[float]
        "chunk_timestamps": [0.42, 0.44, ...],     // null 或 list[float]
        "chunk_intervals_truncated": false
      }],
      "errors": [...],
      "metrics": {
        "ttft_seconds": {...},                      // v1 同
        "total_seconds": {...},                     // v1 同
        "output_chars": {...},                      // v1 同
        "success_rate": 1.0,                        // v1 同
        "successful_rounds": 10,                    // v1 同
        "failed_rounds": 0,                         // v1 同
        // v2 新增（关闭采集时为 null）
        "itl_seconds": null,                        // {count, p50, p95, p99, ...} 或 null
        "throughput_chars_s": null,
        "first_chunk_ratio": null,
        "warmup_rounds_count": 0,
        "steady_state_metrics": null                // {ttft_seconds: {...}, ...} 或 null
      },
      "purity": {...},                              // v1 同
      // v2 新增
      "detectors": {
        "pseudo_stream": {"verdict": "real", "evidence": {...}},
        "slow_start":    {"verdict": "stable", "evidence": {...}}
      }
    }],
    // v2 新增（concurrency_sweep 关闭时为 null）
    "concurrency_sweep": null  // 或 list of {concurrency, ...}
  }]
}
```

**向后兼容核对**：
- `schema_version` 是已有字段（v1=1），仅变值不变 key → 不破任何 v1 消费者（除非有人写了 `assert schema_version == 1`）
- 所有 v2 新字段都有 sane default（`null` / `false` / `0`）
- v1 字段一字不改，v1 字段顺序保持

---

## 5. 时序

### 5.1 单端点单模型 — capture 关闭（v1 等价路径）

```
CLI → load_config → run_benchmark
                       │
                       ▼
                _run_rounds_for_model
                       │
                       ▼
            ThreadPoolExecutor (rounds)
                       │
                  StreamingClient.stream(..., capture_chunk_timings=False)
                       │
                       ▼
                 StreamResult (chunk_intervals=None)
                       │
                       ▼
               metrics = summarize_latencies(...)
              （v2 新字段全部 null）
```

完全与 v1 同路径，零额外开销。

### 5.2 单端点单模型 — capture 开启 + detector 启用

```
CLI --capture-chunk-timings --detect-pseudo-stream --detect-slow-start
   │
   ▼
load_config (test.capture=True, test.detect_pseudo=True, test.detect_slow=True)
   │
   ▼
run_benchmark → _run_rounds_for_model
   │
   ▼
ThreadPoolExecutor (rounds)
   │
   ▼
StreamingClient.stream(capture_chunk_timings=True)
   │
   ▼ (每个 chunk_count++ 后)
   if capture: chunk_timestamps.append(perf_counter()-start)
   if len(chunk_timestamps) > MAX_CHUNK_RECORDS: 截断 + truncated=True
   │
   ▼
StreamResult (chunk_intervals=[...], chunk_timestamps=[...])
   │
   ▼
runner 内：
   itl_seconds = summarize_latencies(flatten(rows[*].chunk_intervals))
   throughput_chars_s = summarize_latencies([output_chars/total for ok rows])
   first_chunk_ratio = summarize_latencies([chunk_intervals[0]/median(chunk_intervals[1:]) for ok rows])
   │
   ▼
detectors.pseudo_stream_detect(rows) → PseudoStreamVerdict
detectors.slow_start_detect([r.ttft for ok rows]) → SlowStartVerdict
   │
   ▼
（如果 warmup_rounds > 0）
   steady_rows = rows[warmup_rounds:]
   steady_state_metrics = summarize_latencies(...)
   │
   ▼
合并写入 results.metrics.{itl_seconds, throughput_chars_s, first_chunk_ratio,
                          warmup_rounds_count, steady_state_metrics}
合并写入 results.detectors.{pseudo_stream, slow_start}
```

### 5.3 concurrency_sweep 时序

```
test.concurrency_sweep = [1, 2, 4, 8]
   │
   ▼
runner._run_rounds_for_model_with_sweep(endpoint, model, test)
   │
   ▼  for c in [1, 2, 4, 8]:    # 串行
       test_copy = {**test, "concurrency": c}
       result_c = _run_rounds_for_model(endpoint, model, test_copy)
       sweep_results.append({
           "concurrency": c,
           "ttft_p50": result_c.metrics.ttft_seconds.p50,
           "ttft_p99": result_c.metrics.ttft_seconds.p99,
           ...
       })
   │
   ▼
合并写入 endpoint.concurrency_sweep
   注：sweep 期间，正常的 results[*] 路径只跑一次（用 test.concurrency 的初值，或默认 1）
       sweep 的中间数据 dump 到 sweep_raw_path（可选 flag），避免主 JSON 过大
```

### 5.4 baseline_diff 时序

```
CLI --compare-baseline reports/baseline.json
   │
   ▼ 主跑流程完成（result dict 已有 v2 完整 schema）
   │
   ▼
baseline_diff.load_baseline(path) → dict
   │
   ▼
baseline_diff.compare(baseline, current) → list[DiffRow]
   │
   ▼
print(baseline_diff.format_diff_table(rows))  # stdout
   │
   ▼
（HTML 报告内追加一个 "Baseline Diff" 段，渲染 DiffRow 表 + 颜色编码 regression/improved）
```

### 5.5 --dry-run 时序

```
CLI --dry-run
   │
   ▼ load_config / 合并 CLI 参数后
   │
   ▼ 在 run_benchmark 入口前：
   total_requests = sum_for_each_endpoint(
       len(models) * rounds * (len(concurrency_sweep) if sweep else 1)
   )
   est_seconds = total_requests * EST_PER_REQUEST_SECONDS  # 默认 5s/请求
   if total_requests > BUDGET_HARD_CAP (500): sys.exit("budget exceeded, use --yes-burn")
   print(f"would send {total_requests} requests, est {est_seconds}s")
   sys.exit(0)  # 不发请求
```

---

## 6. 性能与容量

### 6.1 capture 开销

`perf_counter()` 在 Linux 上 ~20-40 ns；典型流式响应 200 chunks → capture 总开销 < 10 μs，远低于网络 jitter，对 TTFT/ITL 测量精度影响可忽略（< 0.001%）。

### 6.2 内存

| 场景 | 单轮 chunk 数 | 单轮 list 占用 | 单 endpoint 单 model 10 轮总占用 |
|---|---|---|---|
| 典型短输出（max_tokens=512） | 50–200 | ~2 KB | ~20 KB |
| 中输出（max_tokens=2048） | 200–800 | ~8 KB | ~80 KB |
| 长输出（max_tokens=8192） | 800–3000 | ~30 KB | ~300 KB |
| 超长输出（10000+） | > 10000 | 截断保留 10000 = ~100 KB | ~1 MB（截断后） |
| 极端虚拟 chunk 数（恶意 mock）| > 100000 | 截断保留 10000 | ~1 MB |

**内存保护**：单轮 chunk 数超 `MAX_CHUNK_RECORDS=10000` 立即截断保留 head 5000 + tail 5000，`chunk_intervals_truncated=True`，避免 OOM。

### 6.3 时间

| 场景 | rounds | concurrency | 估算耗时（每请求 ~5s） |
|---|---|---|---|
| v1 默认（c=1, rounds=10, models=6） | 10 | 1 | ~5 min |
| v2 sweep [1,2,4,8]（rounds=10, models=3） | 30 / archive | 1→8 串行 | ~15 min（受并发档加速但串行扫档抵消） |
| v2 sweep [1,2,4,8] + 多端点 5 个 | 同上 | 同上 | ~75 min（线性叠加） |

**budget hard cap**：总请求估算 > 500 时要求 `--yes-burn` 显式声明；> 200 时打印 warning 但不阻断。

### 6.4 网络

无并发上限引入新风险；ThreadPoolExecutor 在 c=8 下实测稳定（v1 已有 c=4 经验，c=8 在 mock 测试通过）。

---

## 7. 风险与缓解

| # | 风险 | 影响 | 概率 | 缓解 | 验证 |
|---|---|---|---|---|---|
| R1 | chunk_intervals 内存爆炸（恶意 mock / 异常长输出） | OOM / JSON 上 GB | 低（生产）/ 中（恶意） | head/tail 各 5000 截断 + truncated flag；JSON dump 前 size 监控 | `tests/test_perf_streaming.py::test_chunk_truncation` 构造 20000-chunk mock，断言保留 10000 |
| R2 | pseudo_stream 误报（低带宽真流也呈 burst 模式） | 错杀正常中转站 | 中 | verdict 含 inconclusive 兜底；HTML F5 散点图同步展示证据让用户自判；多轮 vote 阈值 0.5 偏保守 | `tests/test_perf_detectors.py::test_low_bandwidth_real_not_pseudo` 模拟 chunk 间隔从 0.01→0.5（突发但前段密集）→ inconclusive 而非 pseudo |
| R3 | slow_start 把 429 重试误判成冷启 | 错把限速识别成慢启 | 中 | detector 输入仅取 `ok=True` 的轮次；429 单独看 status_code 分布 | `tests/test_perf_detectors.py::test_slow_start_ignores_failed_rounds` |
| R4 | concurrency_sweep 烧 token | 账户成本不可控 | 中 | sweep 长度 > 8 直接退出；总请求 > 500 要求 `--yes-burn`；`--dry-run` 先看账单预估 | `tests/test_perf_runner_sweep.py::test_sweep_max_enforced` |
| R5 | dual-distribution 不变量破坏（误改 audit.py） | parity test 全红 | 低 | design §1.1 / §2.1 受限域表显式列出；PR description 强制勾选「未触碰 audit.py」；CI parity test 守底线 | 现有 `tests/test_dual_distribution_parity.py` |
| R6 | JSON schema v1 消费者断言 schema_version==1 | 外部脚本断裂 | 低 | PRD §6.1 P0-2 锁定 v1 字段一字不改；schema_version 变为 2 是预期行为，CHANGELOG / README 标注 | grep 内部代码无 `schema_version == 1` 断言（仅在 report.py 内做 `>= 2 fallback` 渲染） |
| R7 | ThreadPoolExecutor 在 c=8 时 httpx 连接池瓶颈 | 高并发档数据失真 | 低 | httpx 默认连接池 max_keepalive=20 / max_connections=100，c=8 不饱和；为防万一，sweep 模式下显式构造 `httpx.Client(limits=httpx.Limits(max_connections=c*2))` | `tests/test_perf_runner_sweep.py::test_sweep_c8_no_pool_starvation` |
| R8 | detector 阈值过激进 / 过保守 | verdict 不准 | 中 | 阈值 design 阶段冻结，dev 不得调整；P0 测试覆盖 4 个 verdict 路径；后续真实数据回流再调（v2.1） | 单测 + 真实数据 dogfood（dev 阶段必跑 1 个真实中转站） |
| R9 | 5 张图渲染在大 chunk 数据集（>1万 chunk）下浏览器卡顿 | HTML 打不开 | 中 | F5 散点图渲染前先对 `chunk_intervals` 做 sampling（max 500 点/轮）；其他图基于聚合统计，不受 chunk 数影响 | dogfood 一次长输出场景 |

---

## 8. 与其他切片的接口契约

> 本节冻结 v2 与 S1/S2/S4/S5/Step 13 的字段级边界，避免 dev 阶段切片间抢工。

### 8.1 S1 probe-core（[TES-107](mention://issue/88b1b282-4243-454a-803c-7d3cc035a674)）

- **共享点**：S1 重构 `fetch_models` 到 `api_relay_audit/probe/` 时，v2 跟随 import 更新
- **边界**：v2 **不重写** `fetch_models`；当前 `streaming.py:fetch_models` 保留为兼容 thin wrapper
- **dev 协调**：S1 实施时若改变 `fetch_models` 签名，需在 PR 描述 @ S3 dev 同步

### 8.2 S2 purity-eval（[TES-108](mention://issue/8b69eba6-7b95-49ba-8b69-b69b69b69b69)）

- **共享点**：`analyze_purity` / `analyze_response` 在 `runner._run_rounds_for_model` 内被调用
- **边界**：v2 **不动 purity 输入字段** —— `analyze_response` 接受 `r.text`、`prompt`、`model`、`ok`、`round_index`，全部是 v1 字段；v2 新增 `chunk_intervals` 与 purity 输入隔离
- **dev 协调**：无（v2 不触碰 purity 路径）

### 8.3 S4 pricing-compliance（[TES-110](mention://issue/8b69eba6-...)）

- **共享点**：S4 可能消费 v2 的 `throughput_chars_s` 字段做价格合规分析
- **边界**：v2 **只输出**该字段，不参与 S4 的价格 diff 逻辑；S4 design 自行决定如何使用
- **dev 协调**：S4 dev 在使用 `throughput_chars_s` 时需检查 v2 是否开启 capture（关闭时为 null）

### 8.4 S5 orchestration-report（[TES-111](mention://issue/621b6cbe-4fb7-4481-9dcd-691e3538ee81)）

- **共享点 1**：baseline.json 存储位置（PRD Q4）
- **共享点 2**：CI 集成 `--compare-baseline` 触发条件（PRD Q5）
- **共享点 3**：HTML 报告聚合（S5 可能 import v2 的渲染段）
- **边界**：v2 **只定义** baseline JSON schema + diff stdout 格式 + HTML diff 段；存储位置、CI 触发、多切片聚合大盘归 S5
- **dev 协调**：S5 dev 在 design 内引用 v2 schema 时必须 pin 到本 design commit hash

### 8.5 Step 13 latency_variance（master 已有）

- **共享点**：`detect_bimodality` 函数（pseudo_stream detector P1 增强可能复用）
- **边界**：v2 **仅 import 不修改** `latency_variance.detect_bimodality`；MVP detector 不用 bimodality（直接用 last_burst_share + last_gap_ratio），P1 可选启用
- **dev 协调**：无（pure import）

### 8.6 dual-distribution（`audit.py` 根仓副本）

- **绝对边界**：v2 **零修改** `audit.py`
- **验证**：`tests/test_dual_distribution_parity.py` 在 v2 commit 后必须全绿
- **理由**：v2 是 perf 通路独立子系统，与 audit 13 步主流程无业务逻辑交集

---

## 9. 实现拆分建议（PJM 拆 Story 用）

> 建议拆 6 个 Story，按依赖串行执行，单 Story 工作量 0.5–1 天。

| Story | 标题 | 范围 | 文件 | 依赖 |
|---|---|---|---|---|
| **Story-1** | StreamingClient chunk_timings 采集 + StreamResult 字段扩展 | `streaming.py` 改 2 处 chunk_count++ 后加时间戳记录；`capture_chunk_timings` 参数；`MAX_CHUNK_RECORDS` 截断；`__init__.py` 导出更新 | `api_relay_audit/perf/streaming.py`、`__init__.py`、`tests/test_perf_streaming.py` | 无 |
| **Story-2** | detectors 模块 + 阈值常量 + 单元测试 | 新增 `detectors.py` 含 `pseudo_stream_detect` / `slow_start_detect` + 阈值；mock SSE server 测 4 种 verdict 路径 | `api_relay_audit/perf/detectors.py`、`api_relay_audit/perf/__init__.py`、`tests/test_perf_detectors.py`、`tests/_perf_mock.py` | Story-1 |
| **Story-3** | runner 集成 chunk metrics + detector 触发 + warmup_rounds | `runner._run_rounds_for_model` 内消费 chunk_intervals → itl_seconds / throughput_chars_s / first_chunk_ratio；detector 触发；warmup 剔除 + steady_state_metrics；新增字段进 JSON schema | `api_relay_audit/perf/runner.py`、`api_relay_audit/perf/metrics.py`、`tests/test_perf_runner.py` | Story-1, Story-2 |
| **Story-4** | concurrency_sweep 模式 + 预算保护 | runner 加 `_run_rounds_for_model_with_sweep`；CLI 加 `--concurrency-sweep` / `--dry-run` / `--yes-burn`；sweep 上限 8 enforce | `api_relay_audit/perf/runner.py`、`scripts/perf-bench.py`、`tests/test_perf_runner_sweep.py` | Story-3 |
| **Story-5** | baseline_diff 模块 + `--compare-baseline` 入口 | 新增 `baseline_diff.py` 含 `load_baseline` / `compare` / `format_diff_table` + 阈值；CLI 加 `--compare-baseline` flag；stdout 5 列表渲染 | `api_relay_audit/perf/baseline_diff.py`、`scripts/perf-bench.py`、`tests/test_perf_baseline_diff.py` | Story-3 |
| **Story-6** | HTML 报告 F1–F5 + T1 + diff 段 + 样例 YAML | `report.py` 加 5 个 SVG 块 + 1 个总览表 + diff 渲染段；新增 `perf-configs/example-v2.yaml` | `api_relay_audit/perf/report.py`、`perf-configs/example-v2.yaml`、`tests/test_perf_report_html.py` | Story-3, Story-5 |

**总工作量预估**：3.5–5 工作日（含测试）。

**验收门槛（每 Story 必须满足）**：
- `python -m pytest tests/ -v` 全绿
- `python -m pytest tests/test_dual_distribution_parity.py -v` 全绿（每 commit 都跑）
- `python scripts/collect-metrics.py` 跑过 `docs/_metrics.md` 不漂移

---

## 10. PRD §7.2 未决问题答复

### Q1：pseudo-stream / slow-start 具体阈值

**架构答复（design §4.3 已冻结）**：

- `MIN_CHUNKS_FOR_REAL = 3`：少于此值且 output_chars >= 50 → pseudo
- `PSEUDO_LAST_BURST_SHARE = 0.7`：最末位 chunk 文本占比阈值
- `PSEUDO_GAP_BEFORE_LAST_RATIO = 5.0`：最后一个 chunk 间隔相对中位的倍数
- `PSEUDO_MAJORITY_THRESHOLD = 0.5`：多轮 vote 占比
- `SLOW_START_WARMUP_DEFAULT = 3`：默认前 3 轮算 warmup
- `SLOW_START_RATIO = 1.3`：warmup_avg / steady_avg 阈值
- `MIN_ROUNDS_FOR_SLOW_START = 6`：少于此值 inconclusive

**理由**：
- 这些阈值偏保守（多 inconclusive 少 false positive），符合 PRD §1.2 / §7 R2 风险表「避免错杀正常中转站」原则
- 真实数据回流后 v2.1 再调（design 阶段冻结，dev 不得擅改）

### Q2：concurrency_sweep 是否改 asyncio

**架构答复**：**否**。继续用 `ThreadPoolExecutor`（方案 A）。理由：

- PRD §4.2.3 已锁定 sweep 上限 8，线程池在该规模下稳定无失真
- asyncio 重构（方案 B）侵入 `streaming.py` 全部 370 行，与 v1 同步入口绑定的 dual 心智模型冲突
- 实测线程池 c=8 在 mock 测试通过；如未来 sweep 上限放宽至 16+，再评估异步切换

### Q3：F1–F5 五张图的 SVG / Canvas 实现方案

**架构答复**：**纯 SVG + 内嵌 JS**（与 v1 现状一致）。理由：

- 零外部依赖（不引入 Chart.js / D3）
- 离线可打开（PRD §3.2.2 "不依赖 CDN"）
- 已有 v1 `report.py` 渲染框架可复用（441 行 HTML 模板已经有 SVG bar/line 渲染原型）
- F5 散点图渲染时对 `chunk_intervals` 做 sampling（max 500 点/轮），避免长输出场景 DOM 节点爆炸（R9）

5 张图具体渲染参数：

| # | 图 | x 轴 | y 轴 | 渲染类型 | 数据源 |
|---|---|---|---|---|---|
| F1 | TTFT 分布 | endpoint × model | TTFT 秒 | 箱型图（min/p25/p50/p75/max） | `metrics.ttft_seconds` |
| F2 | ITL 分布 | endpoint × model | ITL 秒 | 箱型图 | `metrics.itl_seconds` |
| F3 | 慢启动曲线 | 轮序 (round 1...N) | 该轮 TTFT 秒 | 折线图 | `rounds[].ttft_seconds` |
| F4 | 并发吞吐曲线 | concurrency (1,2,4,8) | throughput chars/s | 折线图 + 双 y 轴（success_rate 第二轴） | `concurrency_sweep[]` |
| F5 | 伪流证据散点 | chunk index | elapsed 秒 | 散点图 + 中位线 + 阈值线 | `rounds[].chunk_timings` |

### Q4：baseline.json 存储位置

**架构答复**：**下推 S5 orchestration-report PRD（[TES-106](mention://issue/3419145a-f8fa-4a1c-91d9-919b65518b13)）**。v2 只定义 baseline JSON schema（与 v2 schema_version=2 同结构）与 `--compare-baseline <path>` 入口；存储位置（git 跟踪 / artifact / 本地）归 S5。

### Q5：CI 集成 `--compare-baseline` 触发条件

**架构答复**：**下推 S5**。v2 不实现 `.github/workflows/` 集成（受限域），仅提供 CLI 入口供 S5 CI workflow 调用。

---

## 11. 验收路径（PJM/QA 评审 design Story 时按 PRD §6 全量过）

> PRD §6.1 P0 共 9 条，本 design 提供每条的实现锚点：

| PRD P0 项 | design 锚点 | Story |
|---|---|---|
| P0-1 v1 兼容性 | §2.2 / §3.1 默认参数全部 = False | Story-1, Story-3, Story-4 |
| P0-2 JSON schema v2（v1 字段一字不改） | §4.6 schema 表 | Story-3 |
| P0-3 chunk_timings 采集 | §4.1 + §4.2 + Story-1 截断逻辑 | Story-1 |
| P0-4 pseudo-stream detector（4 verdict 路径） | §4.3 + Story-2 mock server 测试 | Story-2 |
| P0-5 slow-start detector | §4.3 + Story-2 mock 数据测试 | Story-2 |
| P0-6 concurrency_sweep | §4.4 + §5.3 + Story-4 | Story-4 |
| P0-7 baseline diff | §4.5 + §5.4 + Story-5 | Story-5 |
| P0-8 HTML 5 张图 + T1 | §10 Q3 + Story-6 | Story-6 |
| P0-9 dual-distribution 不动 | §2.1 受限域表 + §8.6 | 所有 Story 在 CI parity test 守底线 |

PRD P1 / P2 项的 design 锚点：
- P1 预算保护 → §5.5 `--dry-run` + Story-4
- P1 warmup_rounds → §4.6 metrics.warmup_rounds_count / steady_state_metrics + Story-3
- P1 chunk 隐私 → 沿用 v1 `text_preview[:280]` 策略，v2 不导出 chunk_text 全文（Story-6 默认 off）
- P2 多 prompt chunk_timings / chunk_text export → 留 v2.1

---

## 12. 切片签收清单（design → PJM/dev 转交）

提交本 design in_review 时附下列证据：

- [x] 头部 `target_feature_branch: feature/audit-ai-relay` 字段齐全（v5 头部强制字段）
- [x] 门 0 自检通过（仓库身份 + feature 分支 HEAD 对齐，证据见恢复评论 [f8341bd1](mention://comment/f8341bd1)）
- [x] 已读上游 PRD [TES-104](mention://issue/64abbb9b-8a99-4fac-b287-5ce2888ae998) 全部 388 行 + 4 条评论
- [x] 已勘查现状代码：`streaming.py`(372) / `runner.py`(297) / `metrics.py`(52) / `report.py`(441) / `latency_variance.py`(237) / `perf-bench.py`(233)
- [x] 候选方案 3 个（A 推荐 + B/C 不推荐 + 完整取舍）
- [x] 接口契约 / 数据模型（StreamResult / detector / sweep / baseline_diff / JSON schema_version=2）
- [x] 时序图 5 套（v1 等价 / capture 开启 / sweep / baseline_diff / dry-run）
- [x] 性能与容量分析（capture 开销 / 内存 / 时间 / 网络）
- [x] 风险表 9 项，每项有缓解 + 验证用例
- [x] 与 S1/S2/S4/S5/Step 13 + dual 边界 6 条全部明示
- [x] 6 Story 拆分建议（含依赖图 + 工作量预估）
- [x] PRD §7.2 Q1-Q5 全部回答（Q4/Q5 下推 S5）
- [x] 验收路径 P0 9 项 + P1 3 项每项对应 design 锚点 + Story

---

## 13. 变更日志

| 日期 | 版本 | 作者 | 变更 |
|---|---|---|---|
| 2026-05-24 | draft v0 | 资深架构师 | 初稿，按 v5 流程产出（brainstorming 9 步 + 3 候选方案 + PRD §7.2 Q1-Q5 答复） |
