---
issue: [TES-102](mention://issue/1b89ac0d-8043-47c9-99ad-e5ad3d39a910)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
epic: [TES-97](mention://issue/fe5fb483-8cd4-4cc1-8fe3-45f78ff72696)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
slice: probe-core
status: draft
reviewers: 资深产品经理 / 资深架构师 / 项目管理专家
---

# PRD：S1 探测核心（probe-core）

> 版本：v0.2（2026-05-25 PM 收口：F5 退出码 freeze + F1 占位待项目负责人答复 + F2/F3 fixture 落盘）
> 基线代码：master@6693aca（演进式增强，非新建）
> 覆盖切片范围：endpoint 可达性 + auth 形态嗅探 + `/v1/models` 清单 diff + 限速指纹（**明确不含 perf**，归 S3）

### 版本演进

| 版本 | 日期 | 变更 |
| --- | --- | --- |
| v0.1 | 2026-05-24 | 初稿 |
| v0.2 | 2026-05-25 | PJM 解锁 dispatch（TES-93 5/25 13:25 UTC）：（1）§10 退出码语义 freeze（F5）；（2）§7.2 Q2 重写为"待项目负责人答复"占位（F1）；（3）F2+F3 fixture 落盘至 `tests/fixtures/models_diff/`，§4.3 引用更新；本版本仍为 draft，v1.0 升版需等项目负责人对 F1 拍板。 |

---

## 1. 业务背景

### 1.1 行业事实

CISPA Helmholtz《Real Money, Fake Models》（arxiv:2603.01919，2026-03）公开数据：在 24 个被审计的 AI 中转端点中，**45.83% 未通过模型指纹验证**；17 家被学术论文引用的中转服务里 11 家直接基于 one-api / new-api 二开，仅 1 家有正规备案。下游用户在购买 key 时缺少**主动探测能力**，常见的"先充值再发现降级 / 偷换 / 限速"模式造成持续经济损失。

### 1.2 项目立项点

主仓 `yujipeng/audit_ai_api`（fork 自 `toby-bridges/api-relay-audit`，再叠加 `gigi1121/audit_ai_api` 的 perf-bench 增强）已具备 13 步被动审计骨架，覆盖流式完整性 / 提示词注水 / 身份漂移 / 工具替换 / 错误信息泄漏 / latency variance / 框架指纹（infra_fingerprint）等。**真正的缺口在主动探测层**：在不发起昂贵的真模型推理调用前，先用一组低成本探针对中转站做能力声明核对，把"该不该往里灌测试 token"这件事前置成可量化决策。

probe-core 是探测核心层，所有下游评估切片（S2 purity-eval / S3 perf-bench-v2 / S4 pricing-compliance / S5 orchestration-report）的 ground-truth 数据来源都依赖它先把 endpoint 与模型清单核实清楚。**probe-core 失败的中转站，不应该进入下游昂贵评估流水线**。

### 1.3 与现有代码的关系

| 现有模块 | 与 probe-core 关系 |
| --- | --- |
| `api_relay_audit/client.py` | **复用** httpx 通路、SSE 解析、curl fallback；probe 不重建 client |
| `api_relay_audit/infra_fingerprint.py` | **共享** framework 识别签名库；probe-core 的限速指纹复用其 4xx/5xx 头解析风格 |
| `scripts/audit.py` | probe-core 输出作为 audit.py 的 step 0 前置探针，进入 13 步流水线前先打 probe，失败可短路 |
| `tests/test_infra_fingerprint.py` 等 | 沿用既有 mock 风格 + httpx-respx 模式，新增 4 个 test_probe_*.py |

---

## 2. 用户角色与场景

### 2.1 角色

| 角色 | 痛点 | probe-core 提供的价值 |
| --- | --- | --- |
| **AI 中转站采购方**（终端用户） | 充值前不敢先付真金白银做指纹测试 | 一键看到中转站可达性 / auth 形态 / 模型清单合规性 / 限速规则四维数据 |
| **合规 / 风控审计员** | 需要"中转站基础合规"客观证据 | 输出可溯源的探针 raw response 全量记录，附入审计报告 |
| **下游 evaluator 维护者**（项目内） | 不想在每个评估器里重复写"先 ping 再调"逻辑 | probe-core 输出固化为 `ProbeReport` 对象，所有下游 evaluator 引用同一份基线 |

### 2.2 典型场景

**场景 A — 采购前快速尽调（dry-run，免 token 消耗）**

```
用户输入：base_url + 一把待评估 key
probe-core 输出：可达性 / TLS / auth 形态 / 模型清单 vs 官方 diff / 限速规则
判定：通过 → 进入 S2 purity-eval；阻断 → 直接出红色卡片，建议拒绝采购
```

**场景 B — 多家中转站并排尽调（YAML 矩阵）**

```
用户输入：YAML 中列 5 个 endpoint × 1 把 key
probe-core 输出：5 张 ProbeReport，按"可达 / auth 类型 / 清单合规度 / 限速友好度"四维打分
判定：横向对比，导出 `reports/probe-core-<ts>.html`
```

**场景 C — 下游 evaluator 复用基线**（项目内对接）

```
S2 purity-eval 启动前调用 probe.run(base_url, key) → ProbeReport
ProbeReport.models.declared 作为 evaluator 的"应该可调用模型集合"输入
ProbeReport.rate_limit.budget_remaining 作为并发上限的安全阈值
```

---

## 3. 核心流程

### 3.1 探针组合（4 个原子探针 + 1 个聚合器）

probe-core 由 4 个相互独立、可并行、失败可降级的原子探针组成：

| 探针 | 输入 | 输出 | 是否消耗 token |
| --- | --- | --- | --- |
| **P1 reachability**（可达性 + TLS） | base_url | `ReachabilityResult{tcp_ok, tls_ok, tls_chain, dns_resolves, http_status_root, latency_ms}` | 否（全是 GET / HEAD） |
| **P2 auth-sniff**（鉴权形态嗅探） | base_url + key | `AuthSniffResult{accepted_schemes:[bearer\|api_key\|x-api-key\|...], 401_envelope, 403_envelope, key_position}` | 否（一组刻意失败的最小请求） |
| **P3 models-list-diff**（模型清单 diff） | base_url + key | `ModelsDiffResult{declared:[...], official_reference:[...], extra_in_relay:[...], missing_in_relay:[...], suspicious_aliases:[...]}` | 否（仅 GET `/v1/models`） |
| **P4 rate-limit-fingerprint**（限速指纹） | base_url + key | `RateLimitResult{rpm_observed, headers_seen:[...], 429_envelope, retry_after_pattern, burst_window_s}` | **极少**（默认 12 次轻量请求 + 4 次故意触发，可关闭） |

**聚合器 `ProbeReport`**：把 4 个探针结果合并成一份可序列化对象，附 `verdict ∈ {pass, warn, fail}` 整体结论 + 4 个分项判定。

### 3.2 流程图

```
                          ┌── P1 reachability (并行)
input(base_url, key) ─────┼── P2 auth-sniff      (并行)
                          ├── P3 models-list-diff (并行；P1 通过后才执行)
                          └── P4 rate-limit-fp   (并行；P2 通过后才执行)
                                    │
                                    ▼
                          aggregate → ProbeReport
                                    │
                          ┌─────────┴─────────┐
                          ▼                   ▼
                  JSON (machine)       HTML card (human)
                          │
                          ▼
                  下游 evaluator / scripts/audit.py 引用
```

依赖：P3 / P4 不在 P1 / P2 失败时执行（`fail-fast` 默认开），降级模式 `--no-fail-fast` 仍按序触发并把降级原因写入报告。

### 3.3 与现有 audit.py 的接入点

`scripts/audit.py` 现有 13 步流水线：probe-core 作为 **step 0**（pre-flight），输出 ProbeReport 后：

- **verdict=pass** → 进入既有 13 步
- **verdict=warn** → 进入 13 步，但在最终报告头部插警告条
- **verdict=fail** → 短路退出（exit code 2），不进入任何消耗 token 的下游步骤

CLI 兼容：保留所有现有参数；新增 `--probe-only`（仅跑 probe-core 不跑 13 步）和 `--skip-probe`（跳过 probe-core，回到 master 行为）。**默认行为变更**：master 默认无 probe，本切片落地后 audit.py 默认带 probe（fail-fast=on）。

### 3.4 输入契约

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `base_url` | str | ✅ | 完整 URL（含 scheme、含或不含尾 `/v1`，probe-core 自动归一化） |
| `key` | str | ✅ | 真实 key；空字符串走"匿名探测"分支（仅 P1 可执行） |
| `vendor_hint` | enum{auto, openai, anthropic, gemini} | 否 | 默认 auto；影响 P3 的官方清单参考源选择 |
| `proxy` | str | 否 | 代理 URL；走 client.py 既有 httpx 配置通道 |
| `rate_limit_probe.enabled` | bool | 否 | 默认 true；CI 中可关闭以避免 429 风险 |
| `timeout_per_probe_s` | int | 否 | 默认 8 |

### 3.5 输出契约

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-05-24T10:00:00Z",
  "input": {
    "base_url": "...",
    "key_fingerprint": "sha256:8f...",
    "vendor_hint": "auto"
  },
  "verdict": "pass",
  "reachability": { "tcp_ok": true, "tls_ok": true, "latency_ms": 120, "...": "..." },
  "auth_sniff":   { "accepted_schemes": ["bearer"], "401_envelope": "openai-style", "...": "..." },
  "models_diff":  { "declared_count": 84, "extra_in_relay": ["claude-3-5-sonnet-20240620"], "...": "..." },
  "rate_limit":   { "rpm_observed": 60, "429_envelope": "openai-style", "...": "..." },
  "infra_hint":   { "framework": "new-api", "confidence": "confirmed" }
}
```

`infra_hint` **复用** `infra_fingerprint.classify_framework()` 的结果，不重复实现；probe-core 写报告时把它当作派生字段嵌入。

---

## 4. 数据模型骨架

### 4.1 模块新增

```
api_relay_audit/
  probe/
    __init__.py                       # 暴露 run(base_url, key, **opts) -> ProbeReport
    reachability.py                   # P1 实现
    auth_sniff.py                     # P2 实现
    models_diff.py                    # P3 实现 + 官方清单数据源
    rate_limit_fp.py                  # P4 实现
    aggregator.py                     # ProbeReport + verdict 规则
    references/
      official_models_openai.json     # P3 静态参考清单（可被 --models-ref-url 覆盖）
      official_models_anthropic.json
      official_models_gemini.json
```

### 4.2 ProbeReport schema 关键约束

- 所有探针结果**必含** `status ∈ {ok, degraded, error}` 和 `error.code` / `error.message`，禁止 raise 把错误吞掉
- `key_fingerprint` 用 sha256 截前 8 byte 显示，**禁止**任何字段出现 raw key
- `key_position` 字段记 auth header 的位置（Authorization / x-api-key / 自定义），不记 value

### 4.3 官方清单数据源（P3 关键决策）

- **默认**：内置 JSON 静态快照 + 版本号（`api_relay_audit/probe/references/official_models_*.json`，由 dev 在 Story-1 实现时从 `tests/fixtures/models_diff/official_*_catalog.json` 同步迁移），由 PJM 在合并前同步一次官方文档；版本号写入 ProbeReport 便于追溯
- **PM 已交付的 CI fixture（v0.2）**：`tests/fixtures/models_diff/`（PJM TES-93 5/25 13:25 UTC 派单 F2 + F3）
  - `official_openai_catalog.json` / `official_anthropic_catalog.json` / `official_gemini_catalog.json` — 81 个人工策展模型 ID，含 `catalog_version: 2026-05-25` 元数据
  - `case_normal_relay_listing.json` / `case_inconsistent_relay_listing.json` — P3 三类 diff 单测 mock fixture
  - `suspicious_alias_counterexamples.json` — 8 条 suspicious_alias 反例 + 正则种子（dev 据此实现 §3.1 P3 启发式）
  - `README.md` — 文件清单 + provenance + dev 接入约定
- **可覆盖**：`--models-ref-url <url>` 指向外部清单（如官方文档结构化镜像），允许用户使用更新版本
- **不做**：自动爬官方文档（数据源不稳定，由 R3 风险登记防御）

---

## 5. 合规与约束

### 5.1 合规

- **C1** 默认不发起任何带真实推理 payload 的请求；P1/P2/P3 完全不消耗 token；P4 默认仅做 `max_tokens=1` 的最小请求触发限速头
- **C2** `key` 仅以 sha256 前缀写入报告，raw key 不进入 stdout / 任何文件；transparent_log 不记录 key
- **C3** P2 auth-sniff 探针**禁止**把用户 key 发到非 `base_url` 的任何域；只在用户提供的 endpoint 上嗅探
- **C4** rate-limit 探针**默认上限**：12 baseline + 4 burst 请求；超限直接报错而非继续打；CI 模式可一键关闭

### 5.2 非功能约束

- **N1** probe-core 全流程在 endpoint 健康时应在 **8 秒**内出 ProbeReport（4 探针并行 + 8s 单探针超时）
- **N2** 单次运行最大网络流量 < 50 KB（不算 P1 的 TLS handshake）
- **N3** 必须支持 `--via-proxy http://...`，复用 client.py 的 httpx 通道，不重建网络栈
- **N4** 探针实现走"纯函数 + 注入 client"，便于测试 mock；不允许探针内部直接 import `httpx.Client()`

### 5.3 项目约束（v5 规约）

- **PR1** PRD 文件落 `feature/audit-ai-relay`，本文件路径 `docs/prd/2026-05-24-audit-ai-relay-prd-probe-core.md`
- **PR2** PRD 头部含 `target_feature_branch` 字段（已满足，见 frontmatter）
- **PR3** 验收标准每条对应可观测的命令、行为、数据（见第 6 节）

---

## 6. 验收标准（可测）

### 6.1 PRD 切片自身验收（本 PRD 提交流程门）

- [ ] 门 0 自检通过：`git remote -v` 显示 `yujipeng/audit_ai_api` + `git rev-parse --abbrev-ref HEAD` = `feature/audit-ai-relay`
- [ ] PRD 文件 commit 到 `feature/audit-ai-relay` 并 push origin
- [ ] PRD 头部含 `target_feature_branch: feature/audit-ai-relay`
- [ ] TES-102 评论附 commit hash + push stdout + 文件路径

### 6.2 probe-core 切片下游 design / 实现验收（每条对应可观测命令）

- [ ] **A1 模块入口**：`python -c "from api_relay_audit.probe import run; print(run.__doc__)"` 输出非空 docstring
- [ ] **A2 单 endpoint 端到端**：`python scripts/audit.py <base_url> <key> gpt --probe-only --output reports/probe.json` 退出码 0/1/2 三态正确（pass=0, warn=1, fail=2）
- [ ] **A3 ProbeReport schema 校验**：`python -m api_relay_audit.probe.schema_check reports/probe.json` 通过 JSON Schema 校验，`schema_version` 字段存在
- [ ] **A4 P1 reachability 离线 mock**：`pytest tests/test_probe_reachability.py -v` 全绿，覆盖 TCP fail / TLS fail / DNS fail / 200 / 4xx / 5xx 六个分支
- [ ] **A5 P2 auth-sniff 离线 mock**：`pytest tests/test_probe_auth_sniff.py -v` 全绿，覆盖 bearer / x-api-key / 自定义 header / 401 / 403 五个分支
- [ ] **A6 P3 models-diff 静态参考**：`pytest tests/test_probe_models_diff.py -v` 全绿，覆盖 extra_in_relay / missing_in_relay / suspicious_alias 三类 diff
- [ ] **A7 P4 rate-limit 上限保护**：`pytest tests/test_probe_rate_limit.py::test_max_requests` 验证总请求数 ≤ 16；`test_disabled_via_flag` 验证 `--no-rate-limit-probe` 时 P4 跳过且 verdict 不因此 fail
- [ ] **A8 key 不外泄**：`pytest tests/test_probe_no_key_leak.py -v` 通过；正则扫 ProbeReport JSON + stdout，`sk-`、`Bearer ` 等 raw 模式 0 命中
- [ ] **A9 fail-fast 短路**：`audit.py` 在 verdict=fail 时退出码 2，且后续 13 步 step counter 为 0（通过 stderr 日志或 metrics 验证）
- [ ] **A10 与 infra_fingerprint 协同**：ProbeReport.infra_hint.framework 与 `python -m api_relay_audit.infra_fingerprint <base_url>` 独立运行结果一致
- [ ] **A11 性能**：在 mock 健康 endpoint 下 `time python scripts/audit.py ... --probe-only` 实际耗时 ≤ 10s（含 Python 启动 ≈ 2s）
- [ ] **A12 流量上限**：mitmdump 抓包验证 probe-core 总流量 < 50 KB（不计 TLS handshake）
- [ ] **A13 兼容性**：master 模式（`--skip-probe`）行为 100% 与 master@6693aca 一致（diff scripts/audit.py 输出无差异）

### 6.3 与下游切片对接验收

- [ ] **B1** S2/S3/S4/S5 的 design 切片 PRD 引用 `ProbeReport` schema 时，字段命名 / 类型与本 PRD 4.2 一致（架构师交叉评审）
- [ ] **B2** S3 perf-bench-v2 接入 probe-core：跑 perf 前调用 `probe.run(...)`，verdict=fail 时 perf 短路（PJM 验收）

---

## 7. 风险与未决问题

### 7.1 风险登记

| ID | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | P3 官方清单静态快照很快过期 | 假阳性高（误报"中转站多/少模型"） | 内置版本号，CI 每月跑同步脚本，超过 60 天自动告警；用户可 `--models-ref-url` 覆盖 |
| R2 | P4 限速探针在严控 endpoint 上反触发封禁 | 临时被中转站拉黑 | 默认 12+4 上限；提供 `--no-rate-limit-probe` 一键关闭；burst 请求间隔最少 250ms |
| R3 | 中转站对探针请求做"识别后回真模型"对抗（CISPA 论文已有） | P3 假阴性 | probe-core **本切片不解决**，在 S2/S3 由 evaluator 通过抽样 + 真源对照基线（见调研报告 5.4 节）兜底 |
| R4 | base_url 归一化错误（带或不带 `/v1`，trailing slash） | P1/P3 命中错误路径 | 归一化逻辑独立单测覆盖 8 种变体（含 `https://x.com`、`https://x.com/`、`https://x.com/v1`、`https://x.com/v1/`、`http://x.com:8080/api/`...） |
| R5 | 与现有 `audit.py` 的耦合改动可能破坏既有 CLI 用户 | 回归 | A13 验收：`--skip-probe` 必须 100% 兼容 master；CI 加 `audit.py --skip-probe` 烟测固定输出 |
| R6 | P2 auth-sniff 把用户 key 发到错误 endpoint | 凭证泄漏 | C3 强约束 + 单测扫描 P2 实现里所有 httpx 调用的 host 都来自归一化后的 base_url |

### 7.2 未决问题（开放给评审）

- **Q1** 官方清单参考源是否需要内置脚本 `scripts/sync_official_models.py` 自动从厂商文档抓取？（M1：MVP 不做，靠人工同步；M2：考虑内置）
- **Q2** *(F1，2026-05-25 待项目负责人答复)* P4 限速探针默认 baseline：**12 + 4（推荐 / PRD 现状）** vs **仅 12（保守）** vs **默认关闭**。PM 已在父 issue [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c) 评论 [8fb11605](mention://comment/8fb11605) 请示项目负责人，4h 软门后若未答复 PM 默认采用 12+4 并升 v1.0；事后可回滚。**v0.2 锁定结论**：当前 §3.4 / §5.1 C4 / §7.1 R2 一律按 12+4 推进，本 Q2 在项目负责人答复后由 PM 一次性升 v1.0 收口。
- **Q3** ProbeReport 是否单独输出独立 HTML 卡片（`reports/probe-<ts>.html`），还是仅嵌入 audit 主报告？建议 MVP 嵌入 audit 主报告 + JSON 单独可获取，HTML 单独卡片放 S5 编排切片
- **Q4** `--probe-only` 模式是否走独立 CLI 入口（如 `scripts/probe.py`），还是仅作 `audit.py` 的子模式？建议先走子模式（A2），独立入口由 S5 决定

---

## 8. 演进点 vs 主仓现状（对架构师）

| 主仓现状 | probe-core 演进 |
| --- | --- |
| 无主动探测层；audit.py step 1 直接走真模型推理 | 新增 step 0 `pre-flight probe`，4 探针并行，verdict=fail 短路 |
| infra_fingerprint 仅做被动 framework 识别 | 复用其 framework 签名；probe-core 加主动 `/v1/models` + auth + 限速维度 |
| client.py 是最重的依赖（37k LoC 级） | probe-core 不重建 client，所有探针走依赖注入 client |
| 报告侧仅 audit.py 主报告 | ProbeReport JSON 独立产物 + 嵌入主报告卡片，S5 决定是否独立 HTML |

---

## 9. 与父 issue 风险表 / 调研报告对应关系

- **TES-94 P0-1**（连通性 + 鉴权）→ probe-core P1 + P2
- **TES-94 P0-2**（模型清单一致性）→ probe-core P3
- **TES-94 P0-3**（模型指纹声明 vs 实际）→ **不在本切片**（属 S2 purity-eval）
- **架构师 R3**（ground-truth 对照基线）→ 本切片不依赖，输出的 `models_diff` 字段是下游 ground-truth 的输入之一
- **父 issue 风险 R3**（反爬 / WAF）→ 本切片由 P4 上限保护 + `--no-rate-limit-probe` 缓解

---

## 10. 附录：CLI 行为表（v0.2 freeze — F5 收口）

> 来源：PJM TES-93 5/25 13:25 UTC 派单 F5「CLI 退出码语义 freeze 到 PRD §10 表」。本节为 **aggregator Story（[TES-154](mention://issue/da91a840-7bfa-4639-9e6e-fafc8cb3ff18)）实现的唯一退出码契约**，dev 不得自行扩展。

### 10.1 命令矩阵

| 命令 | 行为 | 退出码 |
| --- | --- | --- |
| `audit.py <url> <key> gpt`（默认） | probe-core + 13 步 | **0**（probe pass/warn 都映射为 0，进入 13 步后沿用 13 步退出码） / **2**（probe fail，fail-fast 短路） |
| `audit.py <url> <key> gpt --probe-only` | 仅 probe-core，不跑 13 步 | **0**（pass） / **1**（warn，**warn=1 仅本模式有意义**） / **2**（fail） |
| `audit.py <url> <key> gpt --skip-probe` | 仅 13 步，与 master 一致 | 沿用 master 退出码 |
| `audit.py <url> <key> gpt --no-rate-limit-probe` | 跳过 P4 | 同默认 |
| `audit.py <url> <key> gpt --models-ref-url <url>` | P3 用外部参考清单 | 同默认 |
| `audit.py <url> <key> gpt --no-fail-fast` | probe-core fail 不短路，继续 13 步 + 警告 | 沿用 13 步退出码（不会因 probe verdict=fail 而 exit 2） |

### 10.2 退出码语义（freeze）

| 码 | 含义 | 触发条件 | 出现场景 |
| --- | --- | --- | --- |
| **0** | 成功（含 warn 降级进入 13 步） | (a) 默认模式 probe verdict ∈ {pass, warn} 且 13 步全绿；或 (b) `--probe-only` 且 verdict=pass；或 (c) `--skip-probe` 走 master 路径成功 | 任意模式 |
| **1** | warn — 仅 `--probe-only` 模式有意义 | `--probe-only` 且 probe verdict=warn（某探针 degraded，整体未失败） | **仅 `--probe-only` 模式产出**；默认模式下 verdict=warn 会进入 13 步并最终映射为 0，不产 exit 1 |
| **2** | fail — probe 致命阻断 | (a) 默认模式 probe verdict=fail 触发 fail-fast 短路；或 (b) `--probe-only` 且 verdict=fail | 默认模式 + `--probe-only` 模式；`--no-fail-fast` 抑制默认模式 exit 2 |

### 10.3 实现注意（aggregator dev 收口）

- **R-F5.1** `--no-fail-fast` 优先级高于默认 fail-fast：即使 probe verdict=fail，`--no-fail-fast` 也不产 exit 2，而是把 13 步的退出码透传出去（PRD §3.3 已约束「fail → 短路退出 (exit code 2)」，本附录精化：短路在 fail-fast 启用时才发生）
- **R-F5.2** `--skip-probe` 路径完全绕开 probe-core，退出码语义与 master@6693aca 完全一致（A13 验收）
- **R-F5.3** exit 1（warn）**仅** 出现在 `--probe-only` 模式。默认模式下 verdict=warn 由 reporter 在主报告头部插警告条（PRD §3.3），exit 仍为 0
- **R-F5.4** dev 不得引入 exit code 3 / 4 / 其他自定义码；如需新增（如 "用户主动取消" 等），需先在本表加行 + 走 PRD 升版评审，不得绕过
