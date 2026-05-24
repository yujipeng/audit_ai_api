---
issue: [TES-111](mention://issue/621b6cbe-4fb7-4481-9dcd-691e3538ee81)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
subsystem: orchestration-report
depends_on_prd: [TES-106](mention://issue/3419145a-f8fa-4a1c-91d9-919b65518b13)
status: draft
reviewers: 资深架构师 / 项目管理专家 / 资深开发工程师代表
---

# S5 design — 编排与报告（orchestration-report）

> 基调：**演进式增强**。主仓 `master@6693aca` 已有 13 步审计器 (`scripts/audit.py`) + perf-bench YAML 矩阵 runner (`api_relay_audit/perf/runner.py`) + 透明日志 (`transparent_log.py`) + Markdown reporter + `docs/` GitHub Pages 通路，本切片在既有骨架之上补齐 **跨维度矩阵编排 / 增量缓存 / 基线 diff / 多通道报告 / 凭证安全**，不重写已有检测器。

---

## 1. 上游 PRD 概述

本切片对应 PRD [TES-106](mention://issue/3419145a-f8fa-4a1c-91d9-919b65518b13)，覆盖范围（取自父 issue 切片定义 + PJM 现状勘查 8726aed4）：

- **YAML 矩阵编排**：多 endpoint × 多 model × 多 step（覆盖 S1-S4 检测器）一次跑完
- **增量缓存**：在配置 + 输入未变时复用历史 run 结果，缩短回归时长
- **基线 diff**：与上一已知良好基线对比，回归时只突出变化项
- **静态 HTML 报告**：复用 `docs/` + GitHub Pages 通路，输出 dashboard MVP（无交互 SPA，v2 再议）
- **凭证安全**：加密存储 + 注入隔离 + 日志脱敏（伴随上游 4 个切片 + 本切片所有数据流）

边界外（v2）：交互式 SPA、实时大盘、Webhook 告警仅留 hook 不实装。

---

## 2. 候选方案（按 5 个子模块各列 2-3 个候选 + 取舍）

> 每个子模块的取舍维度：**性能 / 可维护性 / 实现成本 / 演进式约束（双分发不变量）**。

### 2.1 子模块 A — YAML 矩阵 schema

| 候选 | 描述 | 性能 | 可维护性 | 实现成本 | 演进式约束 |
|---|---|---|---|---|---|
| **A1** 复用扩展 perf-bench schema | 在 `perf/runner.py` 现有 `endpoints/test/default_models` 顶层加 `steps: [purity, perf, pricing, probe]` 字段，runner 按 step 列表分派 | 与现状持平 | 单一 schema 易理解；与 perf-bench 强耦合 | 低（增量字段） | ✓ 不改 perf-bench 字段语义 |
| **A2** 新顶层 `audit-suite` schema | 新建 `audit_ai_api/orchestration/` 包，schema 根字段 `version / suite / endpoints / matrix / steps`，把 perf-bench schema 作为一种 step 嵌入 | 同上 | 概念清晰；perf-bench 单跑入口需兼容 | 中（新模块 + 老入口适配） | △ 需保证老 `scripts/perf-bench.py --config` 仍可用 |
| **A3** 混合 — 老 schema 保留作 v1，新 schema 作 v2 | 保留 perf-bench 入口语义，编排器侧识别两种 schema 版本，2 选 1 加载 | 同上 | schema 双轨期复杂度上行 | 中-高 | ✓ 双分发安全，但 audit.py 也需要支持 |

**推荐：A2 + 老入口完全保留**。理由：
- perf-bench schema 当前所有字段都集中在 `test:` 子树，把它整体嵌入 `audit-suite.steps.perf:` 字段语义零损耗
- audit-suite schema 引入 `version: 1`，预留版本迁移点
- `scripts/perf-bench.py --config` 入口保留，原 schema 通过 `_legacy_perf_config_to_suite()` 适配函数升格到 audit-suite 内存模型，避免双轨维护
- 为什么不选 A1：扩展 perf-bench schema 会污染其单跑语义（`test:` 顶层既要存压测参数又要存全局编排参数），中长期可读性差
- 为什么不选 A3：双轨 schema 长期维护成本指数级上升，违反「one source of truth」

**schema 草案**（YAML，最小可用 v1）：

```yaml
version: 1
suite:
  name: relay-audit-default
  description: "Default relay audit suite covering S1-S5"
endpoints:
  - name: relay-A
    base_url: https://relay-a.example.com/v1
    api_key_env: RELAY_A_KEY          # 不存值，引用环境变量
    format: openai                    # openai | anthropic
    vendor: gpt                       # 可选，决定默认 model 列表
    models: [gpt-5.5, claude-opus-4-7] # 显式覆盖 vendor 预设
  - name: relay-B
    base_url: https://relay-b.example.com
    api_key_ref: secret://default/relay_b_key  # 可选，加密 vault 引用
matrix:
  rounds: 10
  concurrency: 2
  timeout: 60
  max_tokens: 512
  prompt: "请介绍北京好吃的"
  warmup: 3
steps:
  - id: probe
    enabled: true
    config: {timeout: 10}
  - id: purity
    enabled: true
    config:
      detectors: [identity, tool_substitution, stream_integrity,
                  error_leakage, web3]
      profile: general
  - id: perf
    enabled: true
    config: {include_legacy_perf_bench_fields: true}
  - id: pricing
    enabled: false                     # P2，本期 MVP 可关
report:
  output_dir: reports/
  formats: [json, markdown, html]
  baseline: reports/baseline.json      # 基线 diff 源
cache:
  enabled: true
  dir: .cache/audit/
  ttl_hours: 168                       # 默认 7 天
```

### 2.2 子模块 B — Run record 数据管道

| 候选 | 描述 | 性能 | 可维护性 | 实现成本 |
|---|---|---|---|---|
| **B1** 扩 TransparentLogger 单文件 | 在现有 `transparent_log.jsonl` 头加 1 行 `run-meta`，每个 step 1 行 `step-result`，请求级 entry 不变 | 单文件 IO 简单 | mixed-row schema 解析复杂；单文件无原子性 | 低 |
| **B2** 双文件（透明日志保留 + run-record.json） | 透明日志保持 hash-only JSONL 不动；新增 `run-record.json` 存运行级元数据（run_id/矩阵坐标/step 聚合/verdict） | 写入轻 | 边界清晰：transparent_log = forensic 证据链；run-record = report 数据源 | 低-中 |
| **B3** SQLite 三表 (runs / steps / requests) | 全本地 SQL 查询，便于跨 run 对比 | 单 run 内多写次开销略升 | 增加 SQLite 依赖（标准库内置但首引入）；schema 迁移成本 | 中 |

**推荐：B2**。理由：
- transparent_log.py 现有协议（hash-only、redact_error、≤1.5KB）已锁定为「forensic 取证链」，与新增 run-record 概念正交，强行混入会破坏其单一职责
- run-record.json 与报告渲染天然 1:1 映射，Markdown/HTML/diff 全部从同一份 JSON 生成
- 不引入 SQLite：本期 MVP 不存在跨 run 复杂查询需求（基线 diff 只对比上一份基线，1:1 JSON-vs-JSON 足够）
- B3 留作 v2 演进点（多 run 历史比对需求出现后再切）

**run-record.json schema 草案**（最小集）：

```json
{
  "schema_version": 1,
  "tool": "api-relay-audit",
  "tool_version": "v2.3-orchestration",
  "run_id": "20260524-031745-a7c3",
  "started_at": "2026-05-24T03:17:45Z",
  "ended_at": "2026-05-24T03:34:12Z",
  "suite_name": "relay-audit-default",
  "suite_digest": "sha256:...",
  "config_digest": "sha256:...",
  "endpoints": [
    {
      "name": "relay-A",
      "base_url_redacted": "https://relay-a.example.com/v1",
      "models_tested": ["gpt-5.5", "claude-opus-4-7"],
      "results": [
        {
          "model": "gpt-5.5",
          "cache_hit": false,
          "steps": {
            "probe":   {"verdict": "pass", "evidence": {...}},
            "purity":  {"verdict": "anomaly", "evidence": {...}},
            "perf":    {"metrics": {...}},
            "pricing": {"skipped": true}
          },
          "overall_risk": "MEDIUM"
        }
      ]
    }
  ],
  "summary": {
    "risk_matrix_6d": {...},
    "n_endpoints": 1,
    "n_models": 2,
    "n_anomalies": 3
  },
  "transparent_log_path": "reports/transparent.jsonl",
  "baseline_diff": {
    "baseline_run_id": "...",
    "changes": [{"endpoint": "relay-A", "model": "...", "step": "purity",
                  "from": "pass", "to": "anomaly"}]
  }
}
```

### 2.3 子模块 C — 增量缓存键

| 候选 | 描述 | 性能 | 可维护性 | 实现成本 | 风险 |
|---|---|---|---|---|---|
| **C1** 粗粒度 run-level | 缓存键 = sha256(suite_digest + endpoints_digest + matrix_digest)；命中复用整个 run-record | 命中率低（任意小改即全失效） | 简单 | 极低 | 实测中很少 100% 复用 |
| **C2** 细粒度 step-level | 键 = sha256(endpoint_name + base_url + model + step_id + step_config_digest + matrix_digest)；命中复用单条 step result | 命中率高（改 1 个 step 不影响其他） | 中等 | 中（cache lookup × step） | TTL 选错会用过期数据 |
| **C3** 分层（粗→细） | 先尝试 run-level，未命中再尝试 step-level，最终细粒度填充 | 命中率最高 | 复杂（两层 cache 一致性） | 高 | 双缓存不一致时调试困难 |

**推荐：C2**。理由：
- 矩阵编排的核心价值在于「同 endpoint × 多 model × 多 step」的笛卡尔积；改一个 step 的 prompt 不应该让所有 step 缓存全失效
- step-level 粒度恰好对应 run-record 的最小可寻址单元（`endpoints[i].results[j].steps[k]`），缓存复用与持久化天然对齐
- 缓存键包含 `base_url` 而非 `endpoint_name`：endpoint name 是 alias 可改，URL 才是身份。但 base_url 进入 cache key 需要先脱敏路径变量（query string）
- 不选 C1：MVP 阶段任何小改都会破缓存，价值约等于零
- 不选 C3：双层 cache 一致性问题维护成本高，且实测 C2 命中率已能覆盖典型回归场景

**缓存键公式**（伪代码）：

```python
def cache_key(endpoint, model, step, suite):
    return sha256("\n".join([
        f"v1",                                   # cache schema version
        f"endpoint:{endpoint.base_url_normalized}",  # 去 query string
        f"format:{endpoint.format}",
        f"model:{model}",
        f"step:{step.id}",
        f"step_cfg:{canonical_json(step.config)}",
        f"matrix:{canonical_json(suite.matrix)}",
        f"tool_version:{TOOL_VERSION}",
    ]).encode()).hexdigest()

def cache_path(key):
    return f".cache/audit/{key[:2]}/{key[2:]}.json"
```

**TTL + 失效策略**：

- 默认 7 天（`ttl_hours: 168`），可在 YAML 覆盖
- 强制 bypass：`audit --no-cache` 或 `cache.enabled: false`
- 强制刷新单 step：`audit --refresh-step purity`
- API key 变化不进 cache key（同 key 跨 endpoint 不会复用，因 base_url 隔离；不同 key 同 endpoint 视为同样的相对真相，复用安全）

### 2.4 子模块 D — 报告渲染

| 候选 | 描述 | 性能 | 可维护性 | 实现成本 | 演进式约束 |
|---|---|---|---|---|---|
| **D1** 演进 `reporter.py` 加 HTML + JSON 渲染器 | `Reporter` 类内增加 `render_html()` / `render_json()` 方法 | OK | 单类多职责，长期会膨胀 | 低 | ✓ 双分发：audit.py 内联同步 |
| **D2** Jinja2 模板驱动 | 数据层 → 模板（markdown.j2 / html.j2 / json.j2）→ 输出；现 Reporter 改为数据收集器 | OK | 视图与数据强解耦 | 中（引入 jinja2 依赖） | ✗ 双分发：audit.py 是 zero-dep curl-only，加 jinja2 破坏 invariant |
| **D3** JSON 作单一持久层，Markdown/HTML 从 JSON 后处理 | `render_json()` 输出 run-record.json；`renderers/markdown.py` / `renderers/html.py` 读 JSON 渲染 | 多次序列化轻微开销 | 边界最清晰，**与 run-record schema 复用** | 中（新增 renderers 包） | ✓ 双分发：renderers 不进 standalone audit.py，仅供模块化版用；standalone 走纯 Reporter |

**推荐：D3**。理由：
- run-record.json 已经是规范化数据源，Markdown/HTML 从同一份 JSON 渲染，**保证 3 种输出的口径绝对一致**
- standalone `audit.py`（curl-only）只需要内置 minimal Markdown 渲染（可复用现有 `Reporter` 类），HTML/diff/dashboard 等高级渲染仅在模块化版可用 → **不破坏双分发不变量**
- 不选 D2：jinja2 是 audit.py 不允许引入的运行时依赖，dual-distribution 强约束
- 不选 D1：单类承担 3 种渲染逻辑长期会膨胀（参考现 Reporter 已有 113 行），分层后每个 renderer ≤200 行

**渲染链路**：

```
runner → orchestrator → run_record(dict)
                              ├─► transparent_log.jsonl     (forensic, 已有)
                              ├─► reports/{run_id}.json     (canonical, D3 新增)
                              ├─► reports/{run_id}.md       (legacy reporter, dual-dist)
                              ├─► reports/{run_id}.html     (静态 HTML, web/ 通路)
                              └─► reports/baseline-diff.md  (基线 diff，子模块 C 输出)
```

**HTML 模板边界**：依 CLAUDE.md «Ask before touching `web/`»，本 design 仅声明 contract（HTML 输出与 `web/` GitHub Pages 通路同源、文件命名约定 `reports/{run_id}.html`），具体 HTML 结构由 web/ 前端 owner 在 PJM 协调下确认（dev 实现阶段必走 PJM-web/前端 owner 三方握手）。

**基线 diff 算法**（伪代码）：

```python
def baseline_diff(current: dict, baseline: dict) -> dict:
    changes = []
    for ep in current["endpoints"]:
        b_ep = find_endpoint(baseline, ep["name"])
        if not b_ep:
            changes.append({"type": "new_endpoint", "name": ep["name"]})
            continue
        for result in ep["results"]:
            b_result = find_model(b_ep, result["model"])
            if not b_result:
                changes.append({"type": "new_model", ...})
                continue
            for step_id, step in result["steps"].items():
                b_step = b_result["steps"].get(step_id)
                if step["verdict"] != b_step.get("verdict"):
                    changes.append({"type": "verdict_change",
                                    "endpoint": ep["name"],
                                    "model": result["model"],
                                    "step": step_id,
                                    "from": b_step["verdict"],
                                    "to": step["verdict"]})
    return {"baseline_run_id": baseline["run_id"], "changes": changes}
```

### 2.5 子模块 E — 凭证安全（加密 + 注入隔离 + 日志脱敏）

凭证安全分 3 个独立子问题：

#### 2.5.1 加密存储

| 候选 | 描述 | 实现成本 | 风险 |
|---|---|---|---|
| **E1.a** 环境变量引用（推荐默认） | YAML 字段 `api_key_env: OPENAI_KEY`，运行时 `os.environ` 读取 | 极低 | 进程内仍是明文，shell history 可能泄漏 → 缓解：禁用 `set -x` 调试输出 |
| **E1.b** 加密配置文件（可选） | YAML `api_key_ref: secret://vault/name`，独立 `secrets.enc` 文件存 AES-GCM 加密的 (name → key) 映射；CLI 启动时 `--master-password` 解密 | 中（密钥派生 + 加解密） | master password 仍需安全输入；忘记导致全部失锁 |
| **E1.c** OS keyring | 走 `python-keyring` 调系统密钥环 | 中-高（依赖跨 OS） | 增加平台耦合，违反 standalone 单文件运行 |

**推荐：E1.a 默认 + E1.b 可选**。理由：
- E1.a 是行业标准做法（GitHub Actions / Docker / 12-factor app），零额外依赖
- E1.b 作为「需要分享配置文件给同事」场景的可选项，AES-GCM + PBKDF2 用标准库 `cryptography`（可选依赖，按需 `pip install cryptography`）
- standalone `audit.py` 只支持 E1.a（curl + env var），E1.b 仅在模块化版可用
- 不选 E1.c：跨平台 keyring 实现复杂，不符合 MVP 范围

#### 2.5.2 注入隔离

**目标**：API key 永远不进入下列任何字段：

- 子进程 argv（`subprocess.run(["curl", "-H", f"Authorization: Bearer {key}"])` ✗，会出现在 `ps` 输出）
- shell 历史（同上）
- exception traceback（`raise RuntimeError(f"call failed with key {key}")` ✗）
- 任何被 reporter / transparent_log 序列化的字段

**实施约束**（写入 design 作为强制规则）：

1. **API key 仅通过显式 kwargs 传递**：所有需要 key 的函数签名采用 `api_key: str` 显式参数（已是 `APIClient.__init__` 现状，需扩展到 orchestrator）；禁止通过全局变量、模块级 `os.environ.get(...)` 读取传递
2. **subprocess 调用强制使用环境变量注入**（不进 argv）：
   ```python
   env = os.environ.copy()
   env["TMP_KEY"] = api_key
   subprocess.run(["curl", "-H", "Authorization: Bearer $TMP_KEY", ...],
                  env=env, shell=False)
   ```
   `client.py` 现已实现此模式，本切片需保证 perf-bench/runner 同样遵守
3. **异常字符串绝不包含 key**：定义 `class CredentialBearer` 包装类，`__repr__` / `__str__` 永远返回 `"<api_key:****>"`；orchestrator 内部所有错误信息走 `CredentialBearer.redacted()` 处理
4. **测试断言**：`tests/test_credential_isolation.py` 增加 fuzz —— 注入 magic key `"SENTINEL-XYZ-DO-NOT-LEAK"`，运行整条链路后 grep `reports/` + `.cache/` + stderr capture 不得出现该字符串

#### 2.5.3 日志脱敏

现 `transparent_log.redact_error()` 只覆盖 `"HTTP 400: ..."` / `"curl failed: ..."` 错误前缀。需扩展：

| 脱敏目标 | 当前 | 增强 |
|---|---|---|
| Authorization header | 不记录（hash-only） | ✓ 保持 |
| 错误字符串 | redact 后第一个冒号 | ✓ 增加 PII 正则二次脱敏（见下） |
| URL query string 中的 token | 未处理 | 新增 `redact_url(url)`，对 `?token=` / `?key=` / `?api_key=` 等参数值替换为 `***` |
| 报告 body 中模型回显的 key | 未处理 | run_record 序列化前过 `redact_response_text(text, sentinel_keys=[run.keys])` |
| stderr 输出 | 未处理 | orchestrator 包装 stderr，所有 print 走 redacted writer |

**通用脱敏正则**（写入 `api_relay_audit/security/redact.py`，新建模块）：

```python
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                  # OpenAI-like
    re.compile(r"sk-ant-[A-Za-z0-9-]{20,}"),             # Anthropic-like
    re.compile(r"Bearer\s+[A-Za-z0-9._-]{20,}", re.I),   # Authorization headers
    re.compile(r"[a-f0-9]{32,}"),                         # 通用 hex token
]
def redact_text(text: str, extra_sentinels: list[str] = ()) -> str:
    for p in SECRET_PATTERNS:
        text = p.sub("***", text)
    for s in extra_sentinels:
        if s and s in text:
            text = text.replace(s, "***")
    return text
```

---

## 3. 取舍与推荐汇总

| 子模块 | 推荐方案 | 关键理由 |
|---|---|---|
| A schema | A2（新顶层 audit-suite，老 schema 适配） | 概念清晰 + 老入口零破坏 |
| B run record | B2（双文件：transparent_log + run-record.json） | 单一职责，与 reporter 共源 |
| C 增量缓存 | C2（step-level 缓存键） | 命中率与改动粒度匹配 |
| D 报告渲染 | D3（JSON 单一源 + Markdown/HTML 后处理） | 不破坏双分发不变量 |
| E 凭证安全 | E1.a 默认 + E1.b 可选 + 注入隔离 4 规则 + 脱敏增强 | 12-factor 默认 + 强约束规则化 |

整体取舍倾向：**复用已有抽象（perf-bench runner、transparent_log、Reporter），把缺口补在「编排层」与「持久化层」**，避免重写 13 步检测器或重新设计已经稳定的协议（双分发不变量、JSONL 取证链）。

---

## 4. 接口契约 / 数据模型

### 4.1 模块拓扑（新增 / 改动）

```
api_relay_audit/
├─ orchestration/             ← 新建包
│  ├─ __init__.py
│  ├─ config.py               # YAML schema 加载 + 校验 + legacy 适配
│  ├─ runner.py               # 矩阵编排主循环
│  ├─ cache.py                # step-level cache（C2）
│  ├─ baseline.py             # baseline diff 算法
│  └─ steps/                  # 各 step 适配层
│     ├─ probe.py             # 调 S1 probe-core（D1 设计中）
│     ├─ purity.py            # 调 S2 purity-eval（D2 设计中）
│     ├─ perf.py              # 调现有 perf/runner.py
│     └─ pricing.py           # 调 S4 price-compliance（D4 设计中）
├─ security/                  ← 新建包
│  ├─ __init__.py
│  ├─ redact.py               # 脱敏正则 + redact_text/redact_url
│  └─ secrets.py              # 环境变量解析 + 可选 AES-GCM 解密
├─ reporting/                 ← 新建包
│  ├─ __init__.py
│  ├─ run_record.py           # run-record.json schema + 序列化
│  ├─ render_markdown.py      # 从 run-record 生成 Markdown
│  └─ render_html.py          # 从 run-record 生成静态 HTML
├─ transparent_log.py         # 现有，扩展 redact_text/redact_url 调用
└─ reporter.py                # 现有 Reporter 保留为 Markdown 渲染底座

scripts/
├─ audit.py                   # 现有 13 步审计器（不改）
├─ perf-bench.py              # 现有，加 legacy schema → audit-suite 适配
└─ audit-suite.py             # 新建：编排入口 = python -m api_relay_audit.orchestration.runner --config relay-audit.yaml

audit.py (root standalone)    # 不接入编排器；保持 zero-dep 单文件，仅消化 Reporter Markdown 输出
```

### 4.2 与上游切片（D1-D4）的接口契约

D5 编排器把 D1-D4 检测器当 step 调用。本切片**只锁定接口形状**，具体检测逻辑由 D1-D4 各自 design 决定：

```python
# orchestration/steps/base.py
class StepResult(TypedDict, total=False):
    verdict: Literal["pass", "anomaly", "inconclusive", "skipped"]
    evidence: dict                 # 自由字段，渲染层只展示
    metrics: dict                  # 性能/计费数值
    error: str | None              # 任何 step crash 都不能终止整条 run

class Step(Protocol):
    id: str                        # "probe" / "purity" / "perf" / "pricing"
    def run(self,
            client: APIClient,
            endpoint: EndpointSpec,
            model: str,
            matrix: MatrixSpec,
            step_cfg: dict,
            cache_get: Callable[[str], Optional[dict]],
            cache_put: Callable[[str, dict], None]
    ) -> StepResult: ...
```

**幂等性要求**：每个 Step.run 必须可重入；同输入应输出同 verdict（用于 cache 一致性验证）。

### 4.3 与下游报告 / web/ 的契约

- `run-record.json` schema 版本字段 `schema_version: 1`，未来 breaking change 必须 bump（与 transparent_log 一致策略）
- HTML 文件命名约定：`reports/{run_id}.html`，与 `docs/_metrics.json` 同目录策略复用 GitHub Pages 通路
- `web/` 现有 dashboard 若需要消化 run-record，PJM 协调前端 owner 在另一个 epic 处理；本 design 仅声明数据契约

### 4.4 CLI 入口

```
python -m api_relay_audit.orchestration.runner \
    --config relay-audit.yaml \
    [--baseline reports/baseline.json] \
    [--no-cache] \
    [--refresh-step purity] \
    [--output-dir reports/] \
    [--format json,markdown,html] \
    [--dry-run]                        # 只解析 schema 不执行
```

向后兼容：`scripts/perf-bench.py --config legacy.yaml` 保留，内部 `legacy_to_suite()` 适配。

---

## 5. 时序图

### 5.1 主路径（无缓存）

```
user                CLI                Orchestrator        Step           Cache         TransparentLog   Reporter
 │                   │                      │                │              │                │              │
 ├─ audit-suite.yaml ▶                      │                │              │                │              │
 │                   ├─ parse + validate ──▶                │              │                │              │
 │                   │                      ├─ for each endpoint, model:    │                │              │
 │                   │                      │   ├─ cache.get(key) ─────────▶│                │              │
 │                   │                      │   │  ◀──── miss ──────────────│                │              │
 │                   │                      │   ├─ step.run(client, ...) ──▶ APIClient calls ▶ log_entry ──▶│
 │                   │                      │   │  ◀── StepResult ──────────┤                │              │
 │                   │                      │   └─ cache.put(key, result) ─▶│                │              │
 │                   │                      ├─ run_record.build() ────────────────────────────────────────▶│
 │                   │                      ├─ baseline.diff(current, baseline)                            │
 │                   │                      ├─ render_markdown / render_html / write run-record.json ──────│
 │                   │  ◀── reports paths ──┤                │              │                │              │
 │  ◀── stdout ──────┤                      │                │              │                │              │
```

### 5.2 增量缓存命中（回归场景）

```
Orchestrator        Cache              Step
 ├─ cache.get(key) ─▶│                  │
 │  ◀── hit ─────────│                  │
 ├─ skip step.run, use cached result    │
 ├─ mark "cache_hit": true in run_record│
```

### 5.3 凭证生命周期（注入隔离）

```
YAML                Orchestrator       APIClient          subprocess(curl)
 ├─ api_key_env ───▶│                   │                  │
 │                  ├─ secrets.resolve()│                  │
 │                  │   reads os.environ│                  │
 │                  ├─ APIClient(key=KEY)──────────────────▶│
 │                  │                   ├─ env["TMP"]=KEY ─▶│
 │                  │                   ├─ curl -H "Authorization: Bearer $TMP" ▶│
 │                  │                   │   (KEY 不进 argv)│
 │                  │                   ◀── response ──────│
 │                  ◀── result (KEY 不进 result) ──────────│
```

---

## 6. 性能与容量

| 指标 | 估算 | 假设 |
|---|---|---|
| 单 step 增量缓存命中延迟 | <5 ms | 本地 SSD JSON 读取 |
| 矩阵编排 overhead | <500 ms / run | 100 个 (endpoint, model, step) 组合的 cache lookup + 调度 |
| run-record.json 大小 | 50-500 KB / run | 5 endpoint × 4 model × 6 step × ~3 KB evidence |
| transparent_log.jsonl 大小 | 30-300 KB / run | 100-1000 请求 × 200-300 B / 行 |
| baseline diff 计算 | <100 ms | 双 JSON 树 walking，N ≤ 500 step 节点 |
| HTML 报告渲染 | <200 ms | 纯字符串模板，无外部依赖 |
| 缓存目录磁盘占用上限 | 100 MB / 7 day TTL | LRU 清理策略 |

并发模型：endpoints 串行，每个 endpoint 内部 `concurrency` 由 matrix.concurrency 控制（继承 perf-bench 既有 ThreadPoolExecutor）。

---

## 7. 风险与缓解

| ID | 风险 | 概率 | 影响 | 缓解 |
|---|---|---|---|---|
| R1 | YAML schema 演进破坏 perf-bench 老入口 | 中 | 高 | `tests/test_legacy_perf_schema_compat.py` 锁旧 schema 字段集；版本字段 schema_version 必填 |
| R2 | 缓存键设计漏字段，回归场景误用过期数据 | 中 | 中 | 缓存键覆盖 step_config 全字段（canonical_json）；TTL 默认 7 天；强制 bypass 入口 |
| R3 | run-record schema 改动破坏下游 web/ 渲染 | 低-中 | 中 | schema_version 凡是 breaking 必 bump；`tests/test_run_record_schema.py` 锁字段集 |
| R4 | 凭证脱敏漏过模型回显的 key | 中 | 高 | sentinel-injection 测试 + SECRET_PATTERNS 双层过滤；CI 集成 `tests/test_credential_isolation.py` |
| R5 | 双分发不变量被破坏（audit.py 被迫加 jinja2） | 低 | 高 | D3 渲染分层确保 standalone 仅依赖 Reporter；CI 已有 `test_dual_distribution_parity.py` 兜底 |
| R6 | HTML 报告与 `web/` 现有 dashboard 冲突 | 中 | 中 | design 阶段不动 `web/`；dev 阶段强制 PJM-web/前端 owner 三方握手（CLAUDE.md «Ask before touching web/»） |
| R7 | step 接口设计过紧，D1-D4 切片实现时被迫破口 | 中 | 中 | StepResult 用 TypedDict total=False，evidence/metrics 自由字段；与 D1-D4 设计代表对齐后再 final |
| R8 | 加密配置（E1.b）冷启动忘记 master password | 低 | 高 | E1.b 是可选项，默认走 env var；E1.b 实施时 README 强调备份 |
| R9 | 增量缓存与 baseline diff 冲突（缓存命中导致 diff 失真） | 中 | 中 | run-record 显式标记 `cache_hit: true`；baseline diff 跳过缓存项或独立标注 |
| R10 | matrix concurrency 高时触发 relay 限流 | 中 | 中 | 继承 perf-bench 现有退避；新增 `matrix.rate_limit_per_minute` 字段（v2 兑现） |

---

## 8. 实现拆分建议（给 PJM 拆 Story 用）

预计 **8 个 Story**，按依赖 DAG 排序：

```
Story-1 (security/redact + secrets)  ──┐
Story-2 (run-record schema + writer)  ─┼─► Story-5 (orchestration runner)
Story-3 (orchestration/config YAML)   ─┤       │
Story-4 (cache step-level)            ─┘       ├─► Story-7 (baseline diff)
                                                └─► Story-6 (Markdown + HTML renderer)
                                                                              │
                                                                              └─► Story-8 (CLI 入口 + e2e 测试)
```

| # | Story 标题 | 关键产出 | 依赖 |
|---|---|---|---|
| Story-1 | `security/redact.py` + `security/secrets.py` + 注入隔离测试 | 4 类正则 + env var 解析 + sentinel fuzz test | 无 |
| Story-2 | `reporting/run_record.py` schema v1 + writer + schema test | TypedDict + json dump + 字段集锁定测试 | 无 |
| Story-3 | `orchestration/config.py` YAML loader + validator + legacy 适配 | audit-suite schema 解析 + perf-bench schema 升格 | 无 |
| Story-4 | `orchestration/cache.py` step-level cache（C2 公式） | get/put + TTL + LRU 清理 + key 测试 | Story-3 |
| Story-5 | `orchestration/runner.py` 主编排循环 + Step 协议 + 4 个 step 适配 | 矩阵 walk + step dispatch + 异常隔离 | Story-1/2/3/4 |
| Story-6 | `reporting/render_markdown.py` + `render_html.py` | 双 renderer + reports/ 目录写入 | Story-2/5 |
| Story-7 | `orchestration/baseline.py` baseline diff + diff renderer | JSON walk + 变更聚合 | Story-2/5 |
| Story-8 | `scripts/audit-suite.py` CLI + e2e 集成测试（最小 fixture） | 入口 + golden file 测试 | Story-5/6/7 |

**双分发同步要点**：本切片 D5 改动**不**进 root `audit.py`（standalone 保持 13 步硬编码 + Reporter Markdown）。dev 实施每个 Story 时若误改 audit.py，CI `test_dual_distribution_parity.py` 会兜底失败。

---

## 9. 与其他切片的接口契约（横向）

| 切片 | 本 design 对其约束 | 其 design 对本约束 |
|---|---|---|
| D1 probe-core (TES-107) | 必须实现 `Step` 协议 `run() -> StepResult`，verdict ∈ {pass, anomaly, inconclusive, skipped} | 决定 `probe` step 的 `step_cfg` 字段（如 timeout、auth schemes） |
| D2 purity-eval (TES-108) | 同上；evidence 字段建议 `{detector_name: detector_result}` 嵌套 | 决定 detector 列表 + profile gating |
| D3 perf-bench-v2 (TES-109) | 适配现有 `perf/runner.py`，把 `_run_rounds_for_model()` 结果包装为 StepResult | perf 字段 metrics 形状（沿用现有 `summarize_latencies`） |
| D4 price-compliance (TES-110) | 同 D1；evidence 建议 `{token_count_official, token_count_actual, ratio}` | 决定价格表数据源接入方式 |

**冲突解决**：若 D1-D4 任一切片需要 StepResult 字段扩展，由对方在其 design 内提案，本 design D5 在 dev 实施期接收 patch；TypedDict total=False 已预留扩展性。

---

## 10. design 完成度检查

- [x] 头部 7 字段齐备（issue / parent_issue / target_repo / target_feature_branch / subsystem / depends_on_prd / status / reviewers）
- [x] 候选方案 ≥ 2-3 个（5 子模块每个均列 2-3 候选 + 取舍）
- [x] 推荐方案附「为什么不选其他」
- [x] 接口契约（StepResult 协议 + run-record schema + 4.2 节）
- [x] 数据模型（YAML schema 草案 + run-record.json schema 草案）
- [x] 时序图（主路径 + cache hit + 凭证生命周期）
- [x] 性能与容量（10 项估算）
- [x] 风险登记（R1-R10 + 缓解）
- [x] 实现拆分建议（8 个 Story DAG）
- [x] 与其他切片接口契约（D1-D4 横向）
- [x] 演进式约束（双分发不变量保持，`web/` 边界尊重 CLAUDE.md）

POC 证据：本切片为「设计层」，性能数字来自 §6 估算（基于现有 perf-bench + transparent_log 实测 IO 量推断）；接口形状无运行时风险（TypedDict + Protocol 都是 type-level 约束）。如 reviewer 认为缓存键或 schema 需 POC，将在 dev 实施 Story-3/4 时补一份 fixture-driven 单元测试作为 POC。
