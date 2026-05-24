---
issue: [TES-106](mention://issue/3419145a-f8fa-4a1c-91d9-919b65518b13)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
epic: [TES-101](mention://issue/71383a3c-d63a-428c-80b3-3eb1c8acd486)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
slice: orchestration-report
status: draft
reviewers: 资深产品经理 / 资深架构师 / 项目管理专家
created: 2026-05-24
---

# S5 PRD — 编排与报告（orchestration-report）

> 切片定位：本切片是 audit-ai-relay 项目的「最后一公里」——把 S1（probe-core）/ S2（purity-eval）/ S3（perf-bench-v2）/ S4（pricing-compliance）四个评估子系统**统一调度起来**，并把跑出来的结果**回归化、人类可读化、可对比化**。
>
> 不解决「测什么」（那是 S1-S4 的事），只解决「怎么把它们一起跑、怎么把结果合起来看、跑出来的东西下次还能不能复用、跑出来的东西能不能放心 commit」。
>
> 演进基调：**演进 `audit.py` / `reporter.py` / `transparent_log.py` / `api_relay_audit/perf/report.py`**，不另起炉灶。

---

## 1. 业务背景

### 1.1 为什么需要本切片

主仓 `master@6693aca` 已具备相对完整的子系统骨架：6 个检测器（identity / tool / error_leakage / stream_integrity / latency_variance / infra_fingerprint）+ perf-bench v1（streaming + purity）+ 静态 HTML 报告通路（perf/report.py）+ JSONL 取证日志（transparent_log.py）。

但当前形态有 4 个关键缺口，限制了项目从「能跑」走向「能持续运营」：

| # | 缺口 | 当前现状 | 影响 |
|---|---|---|---|
| G1 | **多子系统协同** | `scripts/audit.py` 跑 13 步 / `scripts/perf-bench.py` 跑性能；两条独立入口，结果各自落盘 | 想看「同一 endpoint 在 4 个维度下的综合表现」必须串多个命令 + 手工拼装 |
| G2 | **重复跑代价高** | 无缓存层；每次跑都重新打真实 endpoint，消耗 token + 限速指纹累积 | 大矩阵（≥3 endpoint × ≥3 model × ≥4 profile）一次 30 分钟起步；CI 跑不动 |
| G3 | **回归不可观测** | 报告只有"当次"快照，没有"和上次比"维度 | 同行评估、回归发现、长期质量曲线全靠人工对账 |
| G4 | **凭证泄漏面广** | client.py / reporter / transparent_log 多处碰 key、url；redact_error 只覆盖错误字符串 | 报告外发、issue 评论、CI 日志任一环节漏脱敏即 PR 被拒 / 用户 key 暴露 |

### 1.2 与项目主线的关系

本切片是 **DAG 末端节点**：

```
S1 probe-core ─┬─► S2 purity-eval ─┐
               ├─► S3 perf-bench   ─┼─► S5 orchestration-report  ← 本切片
               └─► S4 price-comply ─┘
```

S5 不依赖 S1-S4 的内部实现细节，但**强约束 S1-S4 必须输出一致接口的 CheckResult**（详见 §3.2）。S5 PRD 落地后，S1-S4 的 PRD/design 需要回应该接口约束。

---

## 2. 用户角色与场景

| 角色 | 场景 | 痛点 | 本切片给出的解 |
|---|---|---|---|
| **AI 中转站运维 / 用户**（外部） | 接入新中转站前，想一次性确认「可用性 + 模型清单 + 质量 + 性能 + 价格」是否符合宣传 | 现在要跑 2-3 个脚本、手工对账 | 一个 YAML、一条命令、一份 HTML 报告四维度全展示 |
| **质量门禁 CI**（内部） | 每周自动巡检若干中转站，把回归发现推到 GitHub issue / webhook | 全量跑慢 + 没有「这次和上次比」概念 | 增量缓存命中 ≥80% 时跑完时间 < 5 分钟；baseline diff 自动产 Markdown 摘要 |
| **审计研究员**（内部） | 想验证「中转站 X 在 v1.8.1 → v1.8.2 之间是否发生质量回归」 | 历史报告散落、JSON schema 不稳定 | baseline 二进制制品入库，diff 工具脚本化 |
| **数据合规 / 法务**（内部） | 任何对外发布的报告 / issue 评论 / 日志，必须确保不包含明文 key / 完整 endpoint URL | redact_error 只在 error 路径；HTML 报告 / JSON / log 全链路无统一约束 | redact registry 统一拦截；CI 走 grep gate 强制检验 |

### 2.1 核心流程：跨四维度的「一次跑、一份报告」

```
用户                          orchestrator                        子系统           reporter
  │                                │                                │                 │
  │  perf-configs/myrelay.yaml     │                                │                 │
  ├───────────────────────────────►│                                │                 │
  │                                │  load + validate matrix        │                 │
  │                                │  resolve credentials (env/kc)  │                 │
  │                                │  walk cells (endpoint×model    │                 │
  │                                │              ×profile)         │                 │
  │                                │  ── cache hit? ─► skip exec    │                 │
  │                                │  ── cache miss ─► dispatch    │                 │
  │                                ├───────────────────────────────►│ run profile     │
  │                                │                                │ return Result   │
  │                                │◄───────────────────────────────┤                 │
  │                                │  write transparent_log         │                 │
  │                                │  (redacted, hash-only)         │                 │
  │                                │  store cache entry             │                 │
  │                                │                                │                 │
  │                                │  load baseline (prev run)      │                 │
  │                                │  compute diff per cell         │                 │
  │                                │                                │                 │
  │                                ├──────────────────────────────────────────────────►│
  │                                │  emit HTML (single file) + JSON + diff Markdown   │
  │  out/report.html + .json       │◄──────────────────────────────────────────────────┤
  │◄───────────────────────────────┤                                                   │
```

---

## 3. 核心流程

### 3.1 配置形态：YAML 矩阵

**输入契约**（YAML schema 骨架，详细字段约束见 §4）：

```yaml
schema_version: 1                    # 必填，便于将来演进
run_id: optional-friendly-id         # 可选，用于 baseline diff 配对

matrix:
  profiles: [probe, purity, perf, pricing]   # 启用的能力维度子集（subset of S1-S4）
  defaults:                                  # 矩阵级默认值
    prompt: "请介绍北京好吃的"
    rounds: 3
    timeout: 30
    max_tokens: 512

  models:                                    # 顶层模型集合（endpoint 可覆盖）
    - gpt-5.5
    - claude-opus-4-7
    - claude-sonnet-4-6

  endpoints:
    - name: relay-a
      base_url: https://relay-a.example.com/v1
      api_key: $ENV{RELAY_A_KEY}             # 凭证引用，禁止明文（§5）
      format: openai
      models_override: [gpt-5.5]             # 可选，覆盖顶层 models
      profiles_override: [probe, purity]     # 可选，覆盖 matrix.profiles

cache:
  enabled: true
  policy: layered                            # L1 cell / L2 model-list / L3 pricing
  ttl_models_hours: 24
  ttl_pricing_days: 7
  invalidate_on_code_version: true

baseline:
  enabled: true
  path: .audit-baseline/                     # 仓内目录或可配置（CI 用 artifact）
  diff_thresholds:
    perf_pct: 10                             # 性能数值偏差 ≥10% 标红
    score_drop: 0.1                          # 质量分下降 ≥0.1 标红

report:
  formats: [html, json, markdown_diff]       # 输出形态
  output_dir: reports/
  redact:
    keys: true                               # 永远脱敏（不可关闭）
    urls: host_only                          # 仅保留 host，不保留 path / query
```

### 3.2 子系统集成接口（CheckResult）

S5 不感知 S1-S4 内部实现，仅约束输出契约：

```python
# api_relay_audit/orchestration/result.py（新增）
@dataclass
class CheckResult:
    profile: Literal["probe", "purity", "perf", "pricing"]
    endpoint_name: str
    model: str
    status: Literal["pass", "warn", "fail", "skipped", "error"]
    score: Optional[float]                  # 0.0~1.0；profile 不适用时 None
    metrics: dict[str, Any]                 # profile 自定义结构化指标
    findings: list[Finding]                 # 文本/结构化发现，reporter 渲染用
    raw_artifacts: list[str]                # 落盘的原始日志路径（相对仓库根）
    started_at: datetime
    duration_ms: int
    schema_version: int                     # 与 cache key 联动
```

**S1-S4 各自提供一个 runner adapter**：

```python
# api_relay_audit/orchestration/runners.py（新增，仅声明协议）
def run_probe(endpoint, model, cfg) -> CheckResult: ...
def run_purity(endpoint, model, cfg) -> CheckResult: ...
def run_perf(endpoint, model, cfg) -> CheckResult: ...
def run_pricing(endpoint, model, cfg) -> CheckResult: ...
```

每个 runner 内部可继续调用现有 `api_relay_audit/identity_patterns.py` / `perf/runner.py` 等模块，**S5 本身不动检测逻辑**。

### 3.3 缓存策略（增量）

| 层 | Key | TTL | 失效触发 |
|---|---|---|---|
| **L1 cell result** | sha256(endpoint_url + model + profile + test_input_canonical + schema_version + code_version) | 默认 ∞（按 key 命中） | `--no-cache` / `--refresh` / code_version bump / schema_version bump |
| **L2 model list** | sha256(endpoint_url) | 24h | `--refresh-models` / TTL 过期 |
| **L3 pricing table** | sha256(vendor_name) | 7d | `--refresh-pricing` / TTL 过期 |

- 存储后端：仓内 `.audit-cache/`（默认 gitignore）+ 可选 `--cache-dir` 切到 CI artifact 缓存
- 命中日志：每次 run 打印命中率统计（cells_total / cells_hit / cells_miss）
- **强约束**：缓存 entry 必须包含 `redacted_key_id`（hash 前 8 位）以便审计是否是同一 key 跑出的结果；**绝不写入完整 key**

### 3.4 基线 diff

- **基线存储格式**：JSON snapshot（同 §3.2 CheckResult schema），文件名 `{run_id|date}-{endpoint}-{model}-{profile}.json`
- **diff 维度**：
  - 时序维度：本次 vs 最近一次同 cell 基线
  - 同行维度（可选）：当前 run 内 endpoint A vs endpoint B（同 model + 同 profile）
- **diff 输出**：
  - JSON：machine-readable，CI 消费
  - Markdown 表：人可读，issue 评论 / GitHub Pages 直接贴
  - 触发阈值（§3.1 `diff_thresholds`）超限 → 标红 + diff Markdown 顶部 summary 列出红项

### 3.5 静态 HTML 报告

**MVP 边界（v1）**：

- ✅ 单文件 HTML（embedded JSON via `window.__AUDIT_DATA__`）
- ✅ 离线打开（无后端依赖、无 CDN 资源 / 内联或本地 vendor）
- ✅ 排序 / 筛选 / 折叠（前端 JS，单文件 ≤ 2 MB）
- ✅ baseline diff 视图（红/黄/绿三色 + 数值/分数对比）
- ✅ GitHub Pages 直接 serve
- ❌ 实时大盘 / WebSocket / 服务端渲染（v2 再议）
- ❌ 用户登录 / 权限分级（v2 再议）

**演进**：`api_relay_audit/perf/report.py` 当前只服务 perf 维度，需要泛化为通用 `report/html.py`，支持插槽式 profile section 注入。

### 3.6 凭证安全（贯穿全链路）

详见 §5 合规与约束。

---

## 4. 数据模型骨架

### 4.1 配置文件 schema（YAML）

```python
# api_relay_audit/orchestration/config.py（新增）
@dataclass
class MatrixConfig:
    schema_version: int                       # 当前 = 1
    run_id: Optional[str]
    profiles: list[Literal["probe","purity","perf","pricing"]]
    defaults: TestDefaults
    models: list[str]
    endpoints: list[EndpointConfig]
    cache: CacheConfig
    baseline: BaselineConfig
    report: ReportConfig

@dataclass
class EndpointConfig:
    name: str                                 # 唯一标识，用于 baseline 文件命名
    base_url: str                             # 完整 URL，redact 时只保留 host
    api_key: str                              # 凭证引用表达式（§5），禁止明文
    format: Literal["openai","anthropic"]
    models_override: Optional[list[str]]
    profiles_override: Optional[list[str]]
```

### 4.2 结果归档目录骨架（按 run 隔离）

```
reports/
├─ 2026-05-24T03-22Z-relay-a-myrun/
│   ├─ report.html                          # 主报告
│   ├─ report.json                          # 机器可读快照
│   ├─ diff.md                              # 与上次基线的人读 diff
│   ├─ raw/                                  # transparent log + 原始 artifact（redacted）
│   │   ├─ transparent.jsonl
│   │   └─ relay-a-claude-opus-4-7-purity.json
│   └─ meta.json                            # run_id + code_version + cache 命中统计
```

### 4.3 baseline 目录骨架（受版本控制）

```
.audit-baseline/
├─ relay-a/
│   ├─ gpt-5.5/
│   │   ├─ probe.json
│   │   ├─ purity.json
│   │   ├─ perf.json
│   │   └─ pricing.json
│   └─ ...
└─ relay-b/...
```

### 4.4 缓存目录骨架（不入版本控制）

```
.audit-cache/                              # gitignore
├─ index.json                              # 元数据索引（key → 文件 + 时间戳 + ttl）
├─ cell/<sha256>.json                      # L1
├─ models/<sha256>.json                    # L2
└─ pricing/<sha256>.json                   # L3
```

---

## 5. 合规与约束

### 5.1 凭证安全（强约束，不可妥协）

| 阶段 | 约束 | 实现锚点 |
|---|---|---|
| **配置文件** | YAML `api_key` 字段必须是 `$ENV{NAME}` 或 `@keychain://name` 引用；明文匹配正则 `sk-[A-Za-z0-9]{20,}` → fail-fast 拒绝加载 | `orchestration/config.py` 解析器 + CI lint hook |
| **进程内** | key 仅存在于 `EndpointConfig.resolved_key`（运行期临时字段）+ 子进程 env；不写 args / log / dict-dump | `orchestration/credential.py`（新增） |
| **子进程** | 调度 curl 时通过 env 注入，禁止落在 argv | 复用现有 `client._curl_post`；新增 env-only header 模式 |
| **日志 / 报告 / diff** | 所有外部可读输出经过 redact registry；redact 规则集中维护，覆盖：key（任意形式）/ 完整 URL（仅留 host）/ Bearer token / Authorization header | `orchestration/redact.py`（新增），扩展 `transparent_log.redact_error` |
| **CI 自检 gate** | repo-root grep 任一 commit 不得包含 `sk-[A-Za-z0-9]{20,}` 模式（exclude allowlist） | `.github/workflows/credential-scan.yml`（新增） |

**redact 规则覆盖率验收**：见 §6 验收标准 AC-S5-008。

### 5.2 数据合规

- transparent_log.jsonl 既有 hash-only 设计延续，不变更
- baseline 文件入库前必须经 §5.1 redact registry；CI 自检 gate 同样校验
- 用户 prompt 内容不入 baseline（仅 hash + 元数据）；既有 perf/report.py 的 prompt 嵌入需要在 v1 关闭或仅在本地预览模式下保留

### 5.3 演进兼容性

- `schema_version=1`：本切片落地版本；以后 schema 升级须 bump + 提供 migration（cache 同源失效）
- `code_version`：来自 `git describe --tags --always`，缓存 key 组件之一

### 5.4 法律 / 上游

- 价格表抓取走 S4（pricing-compliance）的官方数据源约束，本切片不直接 scrape
- 探测 endpoint 走 S1 的 robots/限速规约，本切片仅复用结果

---

## 6. 验收标准（可测）

**门 5（Epic 通过）必须全部 √**。每条配命令 / 行为，PJM/QA 直接对照验收。

- [ ] **AC-S5-001 配置加载**：`python scripts/audit-matrix.py --config perf-configs/example-matrix.yaml --dry-run` 在 endpoint=3 / model=3 / profile=4 的矩阵下打印出 36 个 cell 并列出 cache 命中预估，**不发任何网络请求**
- [ ] **AC-S5-002 凭证引用**：YAML 中 `api_key: "sk-abc..."`（明文）应在 load 阶段 fail-fast，错误信息明确指向第几行；`api_key: $ENV{X}` 在 `X` 未定义时也 fail-fast
- [ ] **AC-S5-003 全矩阵执行**：在 mock relay 下跑完 36 cell 矩阵，全部 cell 输出 CheckResult；结果 JSON 通过 schema 校验（jsonschema 或 dataclass 反序列化）
- [ ] **AC-S5-004 增量缓存命中率**：在无 schema/code 变更的前提下，重跑同一 YAML 矩阵，cache hit ≥ 80%（CI 输出 hit/miss 计数）
- [ ] **AC-S5-005 缓存失效语义**：`--refresh-models` 不影响 L1；`--no-cache` 强制全 miss；schema_version bump 全 L1/L2/L3 失效；code_version bump 仅 L1 失效（L2/L3 不受影响，因其与 endpoint/vendor 绑定）
- [ ] **AC-S5-006 baseline diff**：当前 run vs 上一次 baseline，输出 `diff.md` 且阈值超限项（perf 偏差 ≥10% / score 下降 ≥0.1）出现在 Markdown 顶部 summary
- [ ] **AC-S5-007 HTML 报告**：`report.html` 单文件 ≤ 2 MB，浏览器离线打开（断网状态下）渲染完整：四维度 section + baseline diff 视图 + 排序/筛选可用
- [ ] **AC-S5-008 凭证脱敏覆盖**：在测试集上对全部输出制品（report.html / report.json / diff.md / transparent.jsonl / baseline/*.json / stderr）跑 redact-coverage 测试，断言不含 `sk-` / `Bearer ` 明文以及完整 URL（仅 host）；用例覆盖：openai-key / anthropic-key / Bearer token / custom-header key / query-param key
- [ ] **AC-S5-009 并发收益**：全矩阵端到端时间 ≤ sequential 总时长 × 0.6（concurrency=4 时；mock relay 基准）
- [ ] **AC-S5-010 错误恢复**：单个 cell 失败（超时 / 5xx）不阻塞其它 cell；失败 cell 的 CheckResult.status="error"，其余 cell 正常出结果；汇总报告底部列出失败明细
- [ ] **AC-S5-011 GitHub Pages 兼容**：把 `reports/<run>/report.html` copy 到 `docs/audit-report/index.html` 后，GitHub Pages 部署成功且页面正常渲染
- [ ] **AC-S5-012 集成 S1-S4**：在 S1-S4 任一 Epic 至少 1 个 Story done 后，本切片能成功消费其 CheckResult adapter；adapter 单测通过

---

## 7. 风险与未决问题

### 7.1 已识别风险

| 类别 | 风险 | 缓解 | 责任 |
|---|---|---|---|
| **缓存伪绿** | cache hit 但底层 endpoint 已变（被供应商悄悄换模型 / 限速）→ baseline 不更新但实际质量已退化 | (a) cache key 强制纳入 code_version + schema_version; (b) CI 周度 `--no-cache` 强刷一次; (c) baseline 旁路记录上次刷新时间，UI 显示 staleness | dev + ops |
| **凭证漏脱敏** | redact 规则未覆盖某个新格式 key（如 GCP service-account JSON 整段） | (a) redact registry 必须 pluggable + 单测覆盖所有已知 key 形态; (b) AC-S5-008 强制 6+ 用例; (c) CI grep gate 兜底 | dev + 法务 |
| **HTML 报告体积** | 大矩阵 × 多次 baseline 数据膨胀超 2MB → 浏览器卡 / GitHub 拒绝 | (a) 矩阵 cell 超 100 时启用分页加载（embedded JSON 分片）; (b) v2 演进到外部 JSON + lazy fetch | dev |
| **baseline 冲突** | 多 CI runner / 多人并行 push baseline → git merge 冲突 | (a) baseline 文件以 endpoint/model/profile 三键分文件存（已规划，§4.3）; (b) CI 用 `--baseline-readonly` 模式只 diff 不写入 | ops |
| **S1-S4 adapter 漂移** | S1-S4 各自演进时悄悄改 CheckResult.metrics 内结构 → 报告渲染 / diff 误读 | (a) CheckResult.schema_version 强约束; (b) adapter 单测在 S5 Epic 集成 stage 必跑 | 架构师 |
| **mock relay 设施缺失** | AC-S5-003 / AC-S5-009 都依赖 mock relay；S1-S4 各自的 mock 不一定能复用 | (a) S5 Epic 拆 Story 时显式预留 mock-harness Story; (b) 协同 QA 早期对齐 mock 协议 | PJM + QA |

### 7.2 未决问题（待 PJM / 架构师 review 时定）

- **UQ-1**：cache 默认是否启用？倾向「default on，CI 提供 `--no-cache` opt-out」；如果担心首次跑用户被静默缓存，可改成「首次 run 提示 + 写 marker」。
- **UQ-2**：baseline 是否入主仓 / 入哪个分支？  建议方案 A：feature 分支维护 baseline，main 分支不进；方案 B：单独 `audit-baseline` 分支托管 baseline 制品（与代码解耦）。本 PRD 默认采用方案 A；若 PJM 倾向 B，PRD §4.3 与 §3.4 需相应调整。
- **UQ-3**：S5 HTML 报告与现有 `docs/index.html`（v1.8.1 v2 GitHub Pages）合并策略？建议本切片产出独立 `docs/audit-report/`，与现 `docs/` 主页用导航链相连，避免相互覆盖。
- **UQ-4**：redact 规则中「URL 只保留 host」对 self-hosted relay 是否够安全？host 本身是否构成识别？倾向「host 保留 + path/query 全 redact」；如果 host 也敏感（内部 relay），可加 `report.redact.hosts: hash` 模式（host → 8 位 hash 别名）。

### 7.3 提请共识的备选方案

PM 在 brainstorming 过程中评估了以下备选，最终推荐方案见上文主体。其它方案归档备查：

| 维度 | 推荐 | 备选 A | 备选 B |
|---|---|---|---|
| **入口形态** | 新增 `scripts/audit-matrix.py` orchestrator，保留 `scripts/audit.py` / `scripts/perf-bench.py` 现状 | 把现有两个脚本统一改造为子命令 | matrix 作为环境变量驱动现有脚本 |
| **缓存后端** | 文件系统 `.audit-cache/` | SQLite 单库 | Redis（需要外部依赖，被 YAGNI 排除） |
| **HTML 渲染** | 演进 `perf/report.py` → 通用 `report/html.py` | 用 Jinja2 模板（引入新依赖） | 静态站点生成器（mkdocs / docusaurus，超过 MVP 边界） |
| **凭证存储** | env / keychain 引用 | 完全交给环境变量 + YAML 仅占位 | 内建加密 keystore（YAGNI，外部工具已成熟） |

**取舍说明**：推荐方案在「演进兼容性 / 离线友好 / 零外部依赖」三维度全部最优；备选 A 在统一入口上更优，但破坏既有 `audit.py` / `perf-bench.py` 的调用契约（README 多处文档需要重写），代价远高于本切片收益。

---

## 附录 A：与主仓现状的逐项对照

| 主仓现状 | 本切片处理 | 备注 |
|---|---|---|
| `api_relay_audit/client.py` | 复用，不动 | 已有 curl-only 传输适配 |
| `api_relay_audit/reporter.py` | 复用，作为 Markdown diff 生成基础 | 渲染层不变 |
| `api_relay_audit/transparent_log.py` | 演进：redact 规则上移到 `orchestration/redact.py` 统一管理；transparent_log 调用之 | 不破坏现有 hash-only 协议 |
| `api_relay_audit/perf/report.py` | 泛化：拆出通用 HTML 骨架到 `report/html.py`，perf section 作为 profile 插件 | 现有 `--config perf-configs/*.yaml --output xxx.html` 入口保留 |
| `scripts/audit.py` | 保留为 13 步独立入口；新增 `runners.run_probe` 等 adapter 内部复用其检测逻辑 | 不阻塞现有用户 |
| `scripts/perf-bench.py` | 保留；orchestrator 内部经 `runners.run_perf` 复用 perf 模块 | 不阻塞现有用户 |
| `perf-configs/*.yaml` | 兼容：新 schema 在顶部加 `schema_version: 1`；不带版本号的视为 perf-only 旧格式，由 perf-bench 继续消费 | 渐进迁移 |
| `docs/` GitHub Pages 通路 | 复用：本切片 HTML 报告产出独立 `docs/audit-report/` 子目录，与现 `docs/index.html` 并存 | UQ-3 |
| `.gitignore` | 新增 `.audit-cache/` | 缓存不入版本控制 |

## 附录 B：术语表

- **cell**：矩阵中的一个执行单元 = (endpoint, model, profile, test_seed) 四元组
- **profile**：能力维度 = probe | purity | perf | pricing 之一
- **CheckResult**：单个 cell 的执行输出，跨 profile 统一结构
- **baseline**：上一次成功 run 的 CheckResult 快照集合，用于本次 diff 比较
- **run_id**：单次 orchestrator 执行的标识，默认 `{ISO8601-utc}-{endpoint-or-multi}-{shorthash}`
- **redact registry**：集中维护的脱敏规则集合，覆盖 key / URL / Authorization / Bearer / custom header

---

**END OF PRD**
