# 三合一质量检查使用说明

针对 **claude-code-cli** 和 **codex-cli** 实际使用的模型，对 AI API 中转代理做
**性能 + 纯净度 + 13 步安全审计** 的一站式检查。

---

## 适用场景

| CLI 工具 | 实际调用的模型族 | 在本工具中的映射 |
|---|---|---|
| `claude-code-cli` | Claude 4.x (opus / sonnet / haiku) | `vendor: claude`，`format: anthropic` |
| `codex-cli` | GPT-5 系列（含 codex 特化） | `vendor: gpt`，`format: openai` |

> 同一个 relay 即使两端都支持，也要拆成两条记录跑——
> Anthropic 的 `/v1/messages` 和 OpenAI 的 `/v1/chat/completions` 是两套协议。

---

## 1. 准备

```bash
git clone git@github.com:gigi1121/audit_ai_api.git
cd audit_ai_api
pip install -r requirements.txt
chmod +x scripts/full-check.sh
```

---

## 2. 三种用法（按需选一）

### A. 一键三合一（最快）

适合"我就想知道这个 relay 这把 key 整体怎么样"。

```bash
# Claude 通路
scripts/full-check.sh relay-A https://your-relay.example.com sk-ant-XXXX claude

# GPT 通路
scripts/full-check.sh relay-A https://your-relay.example.com sk-XXXX gpt
```

输出归档到 `reports/relay-A-<时间戳>/`：

```
reports/relay-A-20260524-1530/
├── perf.html      性能 + 纯净度，浏览器打开
├── perf.json      结构化数据
└── audit.md       13 步安全审计报告
```

可选参数：

```bash
# 指定审计用的具体模型（默认 claude→sonnet-4-6, gpt→5.3-codex）
scripts/full-check.sh relay-A URL KEY claude claude-opus-4-7

# Web3 场景启用第 11 步注入检测
scripts/full-check.sh relay-A URL KEY claude claude-sonnet-4-6 web3

# 全档（general + web3 全部检测项）
scripts/full-check.sh relay-A URL KEY claude claude-sonnet-4-6 full
```

### B. 多 relay 横向对比（推荐）

适合"我在选 relay，要把 A / B / C 一起跑出对比表"。

编辑 `perf-configs/my-relay-quality.yaml`，按模板加 endpoint：

```yaml
endpoints:
  - name: relay-A-claude
    base_url: https://relay-a.example.com
    api_key: sk-ant-aaa
    vendor: claude
    format: anthropic

  - name: relay-A-gpt
    base_url: https://relay-a.example.com
    api_key: sk-aaa
    vendor: gpt
    format: openai

  - name: relay-B-claude
    base_url: https://relay-b.example.com
    api_key: sk-ant-bbb
    vendor: claude
    format: anthropic
  # ... 想加多少加多少
```

跑：

```bash
python scripts/perf-bench.py \
    --config perf-configs/my-relay-quality.yaml \
    --output reports/comparison.html
```

得到一份 `reports/comparison.html`，所有 endpoint × 模型横向并列。

> 这种方式**不包含 13 步安全审计**——多 relay 安全审计需要分别跑：
> `scripts/full-check.sh relay-B URL KEY claude` 逐个产出。

### C. 单项快速测

只想看某一项时：

```bash
# 只测性能 + 纯净度（10 轮）
python scripts/perf-bench.py URL KEY claude --rounds 10 --output reports/quick.html

# 只跑 13 步安全审计
python scripts/audit.py --key KEY --url URL --model claude-sonnet-4-6 --output reports/audit.md
```

---

## 3. 报告里看什么

### perf.html（性能 + 纯净度）

**性能维度**
- `TTFT` — time-to-first-token。陡升说明伪流式（一次性吐出）
- `p50 / p90 / p95 / p99` — 长尾。p99 抖得离谱 = relay 不稳定
- `mean` — 整体均速

**纯净度维度（6 个检测器）**
| 检测项 | 触发含义 |
|---|---|
| `identity_leak` | 自称错误模型（叫 claude-opus-4-7，回复说自己是 GPT-4 → 红牌） |
| `system_prompt_leak` | 漏出 `[INST]` / `<\|im_start\|>` 等 relay 内部模板标记 |
| `relay_internal_token` | 漏出疑似内部凭据 |
| `language_mismatch` | 中文 prompt 出英文响应（所以默认 prompt 写"请用中文"） |
| `refusal` | 良性 prompt 触发拒答 |
| `empty_response` | HTTP 200 但响应为空 |

每个 endpoint 给一个聚合判定：`clean` / `suspicious` / `injected` / `error`。

### audit.md（13 步安全审计 + 6D 风险矩阵）

| 维度 | 含义 |
|---|---|
| D1 token 注入 | 隐藏 prompt 注入 (AC-1) |
| D2 指令覆盖 | 系统指令被覆盖 / 非 Claude 身份替换 |
| D3 tool-call 替换 | 包名 / 命令被改写 (AC-1.a) |
| D4 错误泄漏 | 错误响应泄漏凭据 / 路径 / 栈 (AC-2 邻近) |
| D5 流异常 | SSE 完整性、usage 单调性、thinking 签名 |
| D6 Web3 注入 | SlowMist 签名隔离（仅 profile=web3/full） |

Step 12（基础设施指纹）/ Step 13（延迟方差）仅供参考，不计入风险矩阵。

---

## 4. 推荐工作流

**初次评估一个 relay**：先 A（一键三合一）跑 Claude + GPT 各一遍，看 audit.md
红牌项决定要不要继续。

**选型对比多个 relay**：用 B（多 relay YAML）跑性能 + 纯净度做横向比较；
对入围的几家再单独跑 A 的安全审计。

**接入后的回归监控**：A 模式 + `cron`，定期跑，对比历史报告归档目录里的
`perf.json` 看趋势。

---

## 5. 相关文档

- `README.md` — perf-bench 通用文档
- `README-upstream-audit.md` — 13 步安全审计原始文档
- `CLAUDE.md` — 项目架构与贡献哲学
- `ROADMAP.md` — 近期候选 / 永久排除项
- `perf-configs/example.yaml` — YAML 字段完整说明
- `perf-configs/four-row-vendor.yaml` — 双 relay × 双 vendor 模板
