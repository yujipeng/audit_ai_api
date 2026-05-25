---
issue: [TES-105](mention://issue/126e1dc5-0bc7-4240-80e1-f7dfde8f3b86)
parent_issue: [TES-93](mention://issue/c8ac1338-5392-4959-9f55-d64745167f4c)
epic: [TES-100](mention://issue/ace05925-7de3-4639-a3c8-a13ee4d30f48)
target_repo: https://github.com/yujipeng/audit_ai_api
target_feature_branch: feature/audit-ai-relay
slice: pricing-compliance
status: draft
reviewers: 资深产品经理 / 资深架构师 / 项目管理专家
---

# PRD 切片 — S4 价格合规（pricing-compliance）

> 本文件为 S4 PRD 切片，覆盖范围：**声称单价 vs 实测 token × 调用 vs 账户扣费偏差 + tokenizer 对齐**。
> 基调：演进式增强（master@`6693aca` 已有 `api_relay_audit/client.py` 的 usage 采集骨架，但**没有任何价格/扣费/tokenizer 交叉验证**）。

---

## 1. 业务背景

### 1.1 攻击面定位

中转站欺诈中「**直接造成用户金钱损失**」的三类手段，全部落在本 PRD 切片：

| 攻击面 | 表现形式 | 用户损失类型 | 当前主仓覆盖 |
| --- | --- | --- | --- |
| **A. 单价不实** | 中转站对外声称 `$0.5 / 1M input tokens`，实际计费按 `$5 / 1M` 扣 | 隐形溢价 10x | ❌ 无 |
| **B. token 虚报** | usage 字段显示 `prompt_tokens=300`，但实际请求只值 `~30 token` | 单次费用虚高 10x | ❌ 无（仅收集，未交叉验证） |
| **C. 账户扣费偏差** | 一组审计调用预期消耗 `$1.20`，账单实际扣 `$2.50` | 累计费用虚高 | ❌ 无（无 balance 探针） |

S2 purity-eval 的「token 注水检测」与攻击面 B 信号重叠但**目标不同**：
- S2 关心「中转站偷偷塞了什么 system prompt」（行为劫持，安全/隐私问题）
- S4 关心「中转站是否多收钱」（金钱损失，财务问题）

两片共用同一份 token-delta 信号源，但归类、阈值、报告位置、责任归属不同。

### 1.2 与 ROADMAP 的关系（红线撤回）

主仓 `ROADMAP.md` 740 行明确将 "Token accounting audit (exact token counting)" 列为 **explicitly NOT doing**，理由：
1. 没有干净的离线 tokenizer
2. 字符比例启发式噪声太大
3. 添加 `tiktoken` 会破坏 zero-dependency invariant（standalone `audit.py` 必须仅靠 Python 3.7+ 与 `curl` 运行）

本 PRD 切片**有限度撤回此红线**，妥协路径见 §5.1。

### 1.3 业务方诉求

- 提供「**这家中转站到底贵不贵 / 是否多收钱**」的可执行结论
- 输出**财务可审**的报告产物（CSV / JSON / HTML），可作为采购选型依据
- 不依赖中转站自报数据（usage 字段、余额接口）作为唯一信号源——交叉验证是核心差异点

---

## 2. 用户角色与场景

### 2.1 用户角色

| 角色 | 主要诉求 | 与本切片的接触面 |
| --- | --- | --- |
| **中转站审计员** | CLI 用户，需要在评估单家中转站时拉出价格审计结论 | `audit pricing --provider <X>` 直接命令 |
| **采购/财务负责人** | 不接触 CLI，消费报告产物 | HTML 报告中的 pricing-compliance section + 摘要 verdict |
| **CI/CD 集成方** | 把价格审计纳入自动化巡检 | 退出码 + JSON artifact + 阈值告警 webhook |

### 2.2 核心场景

#### 场景 ① — 单次评估某中转站定价是否名副其实

- 用户准备引入 `relay-X.example.com`，对方报价 `gpt-4o $2.5 / 1M input tokens`
- 用户在 `audit.yaml` 录入声称单价 + 起始余额快照
- 跑 `audit pricing --provider relay-x --runs 30`
- 报告输出三张表：① token 偏差表 ② 单价折算偏差表 ③ 扣费偏差表
- verdict = `pricing_compliant` / `token_drift_high` / `unit_price_mismatch` / `balance_drift_high` / `inconclusive`

#### 场景 ② — 周期性巡检多家中转站，定位异动

- 用户已有 5 家在用中转站
- 在 CI 上每周跑一次 `audit pricing --all --baseline reports/pricing-baseline-2026-W18.json`
- 输出**与基线对比**的偏差报告；某家从 `pricing_compliant` 转为 `token_drift_high` 时触发 webhook 告警

#### 场景 ③ — 与 S2 purity-eval 联动定位「token 注水」性质

- S2 报告了 token-injection-delta 异常（注水量级 ~120 tokens）
- S4 同次审计中对 usage 字段做了交叉验证，确认中转站是否**还**在按虚高的 usage 计费
- 联合结论：「中转站既偷偷注入 system prompt，又**按注入后的 token 数量收钱**」（双重欺诈）

### 2.3 反向场景（明确不做）

- ❌ 不在线拉取在线汇率（避免被中间人或 metadata 端点投毒；汇率快照由用户在 yaml 中配置）
- ❌ 不主动调用厂商账单 API（如 OpenAI usage endpoint、Stripe）— 中转站层的账单 API 大多不存在或不可信；v1 MVP 只用「中转站自报余额接口（如有）」+「审计前后余额快照差」做扣费验证
- ❌ 不在 v1 MVP 内做月度趋势 dashboard（dashboard 整体由 S5 orchestration-report 承接静态 HTML 即可）

---

## 3. 核心流程

```
[准入] 用户在 audit.yaml 录入：
  ├─ provider 声称单价（input_per_1m / output_per_1m）
  ├─ 货币（USD / CNY，含锁定汇率）
  ├─ 起始余额快照（可选；若中转站暴露余额接口，自动拉取覆盖）
  └─ 审计预算上限（cost_cap_usd，超出立即停手）

         ↓

[执行] 每次 audit 调用：
  ├─ 1. 发送请求 → 收 response（含 usage）
  ├─ 2. 本地 tokenizer 估算 prompt/completion token 数
  │     · 默认：经验估算器（字符/字节比例 + UTF-8 校正，零依赖）
  │     · 可选：tiktoken extras（--enable-token-meter 时启用，需 pip install）
  ├─ 3. 记录交叉数据：
  │     {reported_prompt_tokens, local_est_prompt_tokens,
  │      reported_completion_tokens, local_est_completion_tokens,
  │      claimed_input_unit_price, claimed_output_unit_price,
  │      computed_call_cost = (reported_in × p_in + reported_out × p_out)}
  └─ 4. 累计 expected_total_cost

         ↓

[校准] 跑完一组（默认 N=30）：
  ├─ token 偏差：abs(reported - local_est) / max(local_est, 1) → 分位 + 中位
  ├─ 单价偏差：若中转站暴露 cost 字段，比对 claimed_input × tokens；否则跳过
  └─ 扣费偏差：abs(actual_balance_drop - expected_total_cost) / expected_total_cost
              · actual_balance_drop = balance_snapshot_before - balance_snapshot_after
              · 若中转站不暴露余额接口 → verdict=inconclusive_balance

         ↓

[判定] 三大维度阈值（可在 yaml 覆盖）：
  ├─ token_drift_pct ≤ 20% (warn) / ≤ 50% (high) / > 50% (critical)
  ├─ unit_price_drift_pct ≤ 5% (warn) / > 5% (mismatch)
  ├─ balance_drift_pct ≤ 10% (warn) / > 25% (high) / > 50% (critical)
  └─ verdict = 三者最严级别（critical > high > warn > compliant > inconclusive）

         ↓

[报告] 演进 reporter.py + transparent_log.py：
  ├─ HTML 报告新增 "Pricing Compliance" section（三表 + verdict 徽章）
  ├─ JSON artifact: reports/pricing/<provider>/<run-id>.json
  ├─ 退出码：critical/high → 非 0；warn/compliant/inconclusive → 0
  └─ 可选 webhook：critical 触发飞书/Slack/Email
```

### 3.1 关键计算公式

**token 偏差比**（核心指标，决定单价折算可信度）

```
token_drift_pct = |reported_tokens - local_est_tokens| / max(local_est_tokens, 1) × 100%
```

**单次调用应付费用**（按声称单价折算）

```
expected_cost_per_call = (reported_in × claimed_p_in + reported_out × claimed_p_out) / 1_000_000
```

**扣费偏差比**（核心 verdict 因子）

```
balance_drift_pct = |actual_balance_drop - sum(expected_cost_per_call)| / sum(expected_cost_per_call) × 100%
```

### 3.2 阈值表（v1 默认值，可 yaml 覆盖）

| 指标 | warn | high | critical |
| --- | --- | --- | --- |
| token_drift_pct | > 20% | > 50% | > 100% |
| unit_price_drift_pct | > 5% | > 15% | — |
| balance_drift_pct | > 10% | > 25% | > 50% |

阈值选择依据：
- `token_drift > 20%` 与现有 `docs/python-code-explanation-zh.md` 描述的注水检测黄线（diff > 20）一致
- `balance_drift > 50%` 时实际溢价已 ≥ 1.5×，是采购明确不可接受的红线
- `unit_price_drift > 5%` 接受多币种汇率换算与 round 误差噪声

---

## 4. 数据模型骨架

> 由架构师在 design 切片落地具体类型；本节仅给字段轮廓供下游对齐。

### 4.1 配置项（audit.yaml schema 增量）

```yaml
providers:
  relay-x:
    pricing:
      currency: USD                    # USD / CNY
      input_per_1m_tokens: 2.50        # 声称单价
      output_per_1m_tokens: 10.00
      fx_rate_to_usd: 1.0              # 锁定汇率（USD 时为 1）
      cost_cap_usd: 0.50               # 单次审计预算上限
      balance_endpoint: /v1/dashboard/billing/credit_grants  # 可选；不填则跳过 balance 信号
      balance_jsonpath: $.total_available
      starting_balance_usd: null       # 可选；不填则审计前自动快照
pricing_audit:
  runs_per_provider: 30
  enable_token_meter: false            # tiktoken extras（可选 pip install tiktoken）
  thresholds:                          # 可覆盖默认值
    token_drift_pct: { warn: 20, high: 50, critical: 100 }
    unit_price_drift_pct: { warn: 5, high: 15 }
    balance_drift_pct: { warn: 10, high: 25, critical: 50 }
```

### 4.2 运行时记录（运行内存 / JSON artifact）

```
billing_claim:        provider × model → claimed_p_in / claimed_p_out / currency / fx_rate / observed_at
usage_record:         run_id × call_id → reported_prompt_tokens / reported_completion_tokens
                                       / local_est_prompt_tokens / local_est_completion_tokens
                                       / wall_clock_ms / claimed_cost (if exposed)
balance_snapshot:     provider × phase ∈ {before, after} → claimed_balance / fetched_at / source ∈ {endpoint, manual}
pricing_audit_report: provider × audit_run_id → token_drift_p50 / token_drift_p95
                                              / unit_price_drift_pct (nullable)
                                              / balance_drift_pct (nullable)
                                              / verdict
                                              / verdict_reasons[]
```

### 4.3 报告产物

- `reports/pricing/<provider>/<run-id>.json` — 完整数据，机器可读
- `reports/pricing/<provider>/<run-id>.html` 或合入主报告 — 三表 + verdict 徽章
- 主 HTML 报告 "Pricing Compliance" section — 跨 provider 横向对照表

---

## 5. 合规与约束

### 5.1 zero-dependency invariant 妥协路径

**红线撤回方案**：本切片**不**默认引入 `tiktoken`，而是分两层提供 token 估算：

| 层 | 实现 | 精度 | 默认 | 影响 invariant |
| --- | --- | --- | --- | --- |
| L1 | 经验估算器（字符数 / UTF-8 字节 / 单词数 → 经验比例） | 误差 ±20%~30% | ✅ 默认启用 | 不破坏（纯 stdlib） |
| L2 | `tiktoken` extras | 误差 < 5% | 仅在 `--enable-token-meter` 时启用 | 仅 `requirements-extras.txt`，standalone `audit.py` 不引入 |

L1 在 `token_drift_pct > 50%` 时已足够触发 high，与「20-30% 估算噪声」之间留出安全边际。L2 用于精确量化，不进入 standalone 分发。

**架构师审查项**：本切片需要架构师在 design 切片明确：
- L1 估算器的算法 / 经验系数表 / 测试集
- L2 是否落入 `requirements.txt` 抑或新设 `requirements-extras.txt`
- ROADMAP.md 中 "Token accounting audit explicitly NOT doing" 行的更新方式（注脚 / 移除 / 切片化）

### 5.2 dual-distribution parity（架构不变量）

`api_relay_audit/` 包侧的 pricing 检测器**必须**与 standalone `audit.py` 的等价实现保持 byte-for-byte 一致（沿用现有 `test_dual_distribution_parity`、`test_risk_matrix_character_identical` 的模式）。任何阈值常量、verdict 字符串、报告 schema 字段名都需要 parity test 守护。

### 5.3 计费爆炸防护（沿用主仓现有模式）

- `cost_cap_usd` 在 yaml 中**强制必填**（无默认值，缺失则审计直接拒绝启动）
- 单次审计 runs 上限 `≤ 100`（与 `latency_variance` 的 `[3, 50]` 带宽语义一致，向上放宽到 100）
- 任一调用响应体超 `1 MB` 立即停手（沿用 `error_leakage` 的 256 KB filler 哲学）
- 默认禁用并发，避免在未知中转站上触发限流降级影响计费数据

### 5.4 凭证与单价配置

- 单价、起始余额、cost_cap 一律本地配置，不调用任何在线 metadata 端点（**避免被中转站操控的 `/pricing` 端点投毒**）
- 余额接口 URL / JSONPath 由用户配置；CLI 不内置「猜测余额端点」逻辑
- 本切片不直接处理 API key 加密 — 凭证存储归 S5 orchestration-report 切片的「凭证安全」子能力，本片仅消费已脱敏接口

### 5.5 数据合规

- 报告产物中的 prompt / response 文本默认**截断** + 哈希指纹（保留首 200 字 + sha256 前 12 字符）
- 余额数值在 HTML 报告中按 `$X.XX` 截断到分；JSON artifact 保留原始精度（用户 grep 用）
- 多币种：单一 audit run 内仅允许一种 currency；跨 provider 横向对照在报告层做 USD 折算（按各自 fx_rate）

---

## 6. 验收标准（可测）

> 每条都对应一个可观测命令、行为或数据，避免「估计 / 应该」。

### 6.1 功能验收

- [ ] CLI: `audit pricing --provider <X> --runs 30` 在已正确配置 yaml 的 provider 上跑通，stdout 含 verdict 一行
- [ ] CLI: 缺失 `cost_cap_usd` 时立即退出，stderr 含 `pricing.cost_cap_usd is required`，退出码 ≠ 0
- [ ] CLI: `--enable-token-meter` 未装 tiktoken 时给出明确指引（`pip install audit_ai_api[token-meter]` 或等价），退出码 ≠ 0；不静默回退
- [ ] HTML 主报告含 "Pricing Compliance" section，section 内含三表（token 偏差 / 单价偏差 / 扣费偏差）+ 顶部 verdict 徽章
- [ ] JSON artifact 路径 `reports/pricing/<provider>/<run-id>.json` 存在且 schema 通过 `tests/test_pricing_artifact_schema.py`

### 6.2 准确性验收

- [ ] 喂入 fixture：报告 usage `prompt_tokens=100`、本地估算 100 → token_drift_pct=0%，verdict 含 `pricing_compliant`
- [ ] 喂入 fixture：报告 usage `prompt_tokens=300`、本地估算 100 → token_drift_pct=200%，verdict 含 `token_drift_critical`
- [ ] 喂入 fixture：声称 `$2.5/1M`、计算结果与中转站 cost 字段差 4% → unit_price_drift_pct=4%，无 mismatch
- [ ] 喂入 fixture：声称累计 `$1.00`、实际 balance 下降 `$1.30` → balance_drift_pct=30%，verdict 含 `balance_drift_high`
- [ ] 多币种 fixture：CNY × fx_rate=0.14 → USD 折算差 < 1%

### 6.3 不变量验收

- [ ] `test_dual_distribution_parity` 扩展，覆盖 pricing 检测器：standalone `audit.py` 与 `api_relay_audit/pricing/*.py` 的常量/verdict 字符串一致
- [ ] `test_zero_dependency_invariant`（新增）：默认 install（不带 extras）能跑通 `audit pricing` 主路径，仅 L1 估算器
- [ ] `test_billing_safety_invariants`（新增）：① cost_cap 缺失退出 ② runs > 100 退出 ③ 响应体 > 1 MB 截断
- [ ] ROADMAP.md 更新：「Token accounting audit」行在 "Explicitly NOT doing" 表中要么被移除并迁入 "Recently shipped"，要么标注「partial — pricing-compliance slice with bounded-precision L1 + opt-in L2」

### 6.4 集成验收（对齐 S2 / S5）

- [ ] 与 S2 purity-eval 共用 token-delta 信号源：S2 的 token-injection-delta 与本切片的 token_drift 在同一次 audit run 中**结果一致**（同 fixture 同算法），由 `tests/test_token_signal_consistency.py` 守护
- [ ] 报告嵌入：S5 orchestration-report 的 YAML 矩阵能编排 `pricing` 检测器，缓存键含 `(provider, model, runs, thresholds_hash)`
- [ ] webhook 兼容：critical/high verdict 通过 S5 提供的 `webhook` hook 推送（本切片不重复实现）

### 6.5 可观测性验收

- [ ] 退出码：critical/high → 1，warn → 0 (但日志含 WARN)，compliant/inconclusive → 0
- [ ] `transparent_log` 内出现 `pricing.token_drift / pricing.balance_drift` 字段
- [ ] `--verbose` 模式下打印每次调用的 (reported, local_est, drift) 三元组

---

## 7. 风险与未决问题

### 7.1 已识别风险

| ID | 风险 | 影响 | 缓解 |
| --- | --- | --- | --- |
| R1 | L1 经验估算器精度仅 ±20-30%，对临界 case（drift 近阈值）误判 | verdict 误标 warn ↔ compliant | 阈值默认值留余量；用户可通过 `--enable-token-meter` 升级到 L2；HTML 报告显式展示估算层级 |
| R2 | 中转站不暴露余额接口（多数国内中转站无此 API） | balance_drift 信号缺失，verdict 退化 inconclusive_balance | 视为正常退化路径；token_drift + unit_price_drift 仍可独立给结论 |
| R3 | 多币种汇率噪声 / 配置错误 | 跨 provider 横向对照失真 | 锁定 fx_rate 由用户配置；不做在线汇率拉取；报告显式标注「汇率快照时间」 |
| R4 | 与 S2 purity-eval 的 token-injection-delta 信号冲突（同一 token 偏差既算注水又算计费虚高） | 用户报告读者混淆 | §2.2 场景 ③ 明确联合判定逻辑；S2 / S4 报告 section 互链 |
| R5 | ROADMAP "Token accounting audit explicitly NOT doing" 红线撤回需 ARCHITECTURAL invariant 文档更新 | 主仓贡献者认知不一致 | 由架构师在 design 切片同步更新 ROADMAP；新增 invariant: dual-precision tokenizer pluggability |
| R6 | 小样本（runs=30）下扣费偏差信噪比低（中转站可能仅在大样本下才表现差异） | balance_drift verdict 误判 compliant | 文档建议 weekly 巡检 runs ≥ 200；CI baseline 模式默认 runs=100 |
| R7 | 中转站可能识别审计指纹后切换为「合规模式」 | 审计结果被对抗污染 | 与 S1 probe-core 的 client-fingerprint randomization 联动（属 S1 范围，本片仅消费） |
| R8 | tiktoken extras 升级 / 模型 tokenizer 变更（GPT-5 用了新 tokenizer） | L2 估算与厂商不一致 | tiktoken 版本锁定 + 每次 `audit pricing` 启动校验 tokenizer 版本，必要时给 warn |

### 7.2 未决问题（待 design / dev 阶段或 v2 决策）

- **U1**：是否在 v1 MVP 即支持 OpenAI usage API / Stripe / Anthropic usage 等厂商账单上拉？
  - **PM 倾向**：否。中转站层的「上游真实账单」对终端用户不可见且不可信。v2 视用户反馈再议。
- **U2**：`audit pricing` 是否拆为独立子命令，还是合入主 `audit run` 的 `--include pricing` 矩阵？
  - **PM 倾向**：独立子命令为主入口（场景 ① 需求明确），主 `audit run` 矩阵中也允许引用（场景 ② 巡检需要）。最终归 S5 orchestration-report 切片决定 yaml 矩阵 schema。
- **U3**：余额信号缺失时是否允许用户**手动**录入 `before / after balance`（CLI 交互问询）？
  - **PM 倾向**：可，但仅在 `--interactive` 模式启用；非交互模式（CI）严格走 endpoint，缺失即 inconclusive。
- **U4**：HTML 报告中是否需要一个跨 provider 的「价格性价比」排名（每 100 token 实际成本最低 → 最高）？
  - **PM 倾向**：v1 仅给原始数据 + verdict，不输出排名（避免**误导**采购决策；价格仅是采购维度之一，质量/合规更重要）。v2 可考虑「pareto front」可视化。
- **U5**：货币种类是否扩展到 EUR / JPY / KRW（中转站市场最常见 USD / CNY 之外）？
  - **PM 倾向**：v1 仅 USD + CNY，schema 预留扩展位（`currency` 字段为 ISO 4217 字符串，不限枚举）。

---

## 8. 与其他切片的接口

### 8.1 上游依赖

- **S1 probe-core**：endpoint 可达性 + auth 嗅探（`audit pricing` 不重复实现连通性检查）
- **凭证读取**：来自 S5 orchestration-report 的「凭证安全」子能力（本片仅消费已脱敏接口）

### 8.2 下游被消费

- **S5 orchestration-report**：YAML 矩阵编排可引用 `pricing` 检测器；HTML 主报告整合 pricing section
- **S2 purity-eval**：共用 token-delta 信号源（实现在公共模块 `api_relay_audit/token_estimator/`），S2 调用其计算注水规模、S4 调用其计算 token_drift_pct

### 8.3 横向对齐

- **S3 perf-bench-v2**：本片不重复 latency 维度；perf-bench 的 latency_variance 不重复 cost 维度
- **存储**：JSON artifact / HTML / transparent_log 均落 `reports/` 下（与现有惯例一致）

---

## 9. 演进点 vs 主仓现状

| 维度 | master@`6693aca` 现状 | 本切片演进 |
| --- | --- | --- |
| usage 字段采集 | ✅ `client.py` 已收集 input/output_tokens（行 124, 159, 363, 394） | 新增本地交叉验证层 |
| token 估算器 | ❌ 无独立模块 | 新增 `api_relay_audit/token_estimator/`（L1 stdlib + L2 tiktoken extras） |
| 单价/扣费数据模型 | ❌ 无 | 新增 `api_relay_audit/pricing/{claims,records,verdict}.py` |
| 报告 section | ❌ 无 pricing section | 演进 `reporter.py` + `transparent_log.py` |
| ROADMAP 红线 | ❌ "Token accounting audit explicitly NOT doing" | 撤回，新增 dual-precision invariant |
| billing-cap 防护 | ✅ 已在 `latency_variance.py` / `error_leakage.py` 内联 | 复用同一哲学，新增 `cost_cap_usd` 强制必填 |
| dual-distribution parity | ✅ `test_dual_distribution_parity` 守护 | 扩展 parity 覆盖 pricing 模块 |

---

## 10. 不在本 PRD 范围内（避免 scope creep）

- ❌ 在线汇率查询（FX rate）
- ❌ 厂商上游账单 API 集成（OpenAI usage / Stripe / Anthropic）
- ❌ 多用户权限分级（user/admin 看不同字段）
- ❌ 实时大盘 dashboard（v1 MVP 静态 HTML 即可，归 S5）
- ❌ 月度 / 季度趋势分析（v2 再议）
- ❌ pareto 性价比排名（v2 再议）
- ❌ 新增非 USD/CNY 货币的内置支持（schema 预留即可）

---

## 11. v5 流程合规自检

- [x] PRD 头部强制字段齐备（issue / parent_issue / target_repo / target_feature_branch / slice / status / reviewers）
- [x] 验收标准每条对应可观测命令 / 行为 / 数据
- [x] 与 S2 / S5 的接口已显式定义
- [x] 不重写 design.md / plan.md（架构师在 D4 design 切片落地）
- [x] 演进式增强基调（master@`6693aca` 现状已盘点，演进点已映射）
- [x] 文件命名 `2026-05-24-audit-ai-relay-prd-pricing-compliance.md` 符合规约
- [x] 落盘路径 `docs/prd/`（PRD 切片专用目录，与 docs/ 现有内容并列，避免污染主仓现有文档命名空间）
