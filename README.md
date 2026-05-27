# audit_ai_api

AI API 中转 (relay) 性能 + 验纯一体化测试工具。

> Fork 自 [toby-bridges/api-relay-audit](https://github.com/toby-bridges/api-relay-audit) ——
> 在原仓库的 13 步安全审计基础上，**新增了 perf-bench**：流式 TTFT / p95 / p99 性能压测 + Prompt 注入 / 身份漂移验纯，
> 用同一批响应同时算两类指标，一份 HTML 报告对比多家 relay。

---

## 它能做什么

- ⚡ **性能压测**：流式抓取 TTFT、平均耗时、p50/p90/p95/p99，识别伪流式 / 慢启动
- 🧪 **验纯**：身份漂移、System prompt 泄漏、relay 内部 token、语言不匹配、空响应、refusal —— 全部用性能测试同一批响应做检测
- 🌐 **多路对比**：YAML 配置任意多 endpoint × 多模型，一份 HTML 报告横向比较
- 🪪 **vendor 预设**：`gpt` 一键展开成 GPT-5 家族、`claude` 一键展开成 Claude 4 家族
- 🚨 **异常归档**：超时 / 4xx / 5xx 全部进报告（含 request_id、HTTP 体），不丢任何失败现场
- 📦 **零部署**：HTML 报告单文件，浏览器双击即看；JSON 同名输出便于二次分析

---

## 安装（开箱即用 60 秒）

```bash
git clone git@github.com:gigi1121/audit_ai_api.git
cd audit_ai_api
pip install -r requirements.txt        # httpx + PyYAML

# 跑通验证：单 endpoint，gpt 厂商，3 轮，默认 prompt = 请介绍北京好吃的
python scripts/perf-bench.py https://your-relay.example.com sk-yourkey gpt \
    --rounds 3 --output reports/quick.html
```

跑完会得到 `reports/quick.html`（带可视化对比表）和 `reports/quick.json`（结构化数据）。

---

## 三种用法

### A. 单路 — 位置参数最简形式

```bash
python scripts/perf-bench.py <url> <key> <vendor> [选项]
```

`vendor` 取 `gpt` 或 `claude`，自动展开成代表模型集合。

```bash
# 测一把 key 在 GPT 通路上的表现
python scripts/perf-bench.py https://relay.example.com sk-XXXX gpt \
    --rounds 10 --output reports/relay-gpt.html

# 测同一 relay 的 Claude 通路
python scripts/perf-bench.py https://relay.example.com sk-XXXX claude \
    --rounds 10 --output reports/relay-claude.html
```

### B. 多路 — YAML 一次跑完

```bash
python scripts/perf-bench.py --config perf-configs/four-row-vendor.yaml \
    --output reports/comparison.html
```

YAML 模板 (`perf-configs/example.yaml`)：

```yaml
test:
  prompt: "请介绍北京好吃的"
  rounds: 10
  concurrency: 1     # >1 时同模型并发批量
  timeout: 30
  max_tokens: 512
  format: openai     # 或 anthropic
  # temperature 留空 = 不传（兼容 claude-opus-4-7 等不接受温度的模型）

endpoints:
  - name: relay-a
    base_url: https://relay-a.example.com
    api_key: sk-aaaa
    vendor: gpt        # 自动展开 gpt-5.2 / 5.3-codex / 5.5

  - name: relay-b
    base_url: https://relay-b.example.com
    api_key: sk-bbbb
    vendor: claude     # 自动展开 claude-haiku-4-5 / sonnet-4-6 / opus-4-7

  - name: relay-c
    base_url: https://relay-c.example.com
    api_key: sk-cccc
    models: [gpt-5.5, claude-sonnet-4-6]   # 显式 models 覆盖 vendor
```

### C. 自定义 prompt / 模型 / 轮数

```bash
# 自定义 prompt
python scripts/perf-bench.py URL KEY gpt --prompt "讲一个简短的笑话" --rounds 5

# 多 prompt 轮换（每行一条）
python scripts/perf-bench.py URL KEY gpt --prompts-file prompts.txt --rounds 12

# 显式指定模型（覆盖 vendor 预设）
python scripts/perf-bench.py URL KEY gpt --model gpt-4o-mini --model o1-mini

# 并发批量
python scripts/perf-bench.py URL KEY gpt --rounds 30 --concurrency 5
```

---

## vendor 预设

| vendor   | 展开模型                                                             |
| -------- | -------------------------------------------------------------------- |
| `gpt`    | `gpt-5.2`, `gpt-5.3-codex`, `gpt-5.5`                                |
| `claude` | `claude-haiku-4-5`, `claude-sonnet-4-6`, `claude-opus-4-7`           |

任意时候用 `--model M`（CLI，可重复）或 `models: [...]`（YAML）覆盖。

---

## 验纯做了什么

每条流式响应都过一遍 6 类启发式检测；多轮聚合后给一个 verdict。

| Issue                  | 触发条件                                                                                |
| ---------------------- | --------------------------------------------------------------------------------------- |
| `identity_leak`        | 响应里有"我是 GPT" / "I am Claude" 之类**带身份锚点**的非目标模型自报家门                   |
| `system_prompt_leak`   | `[INST]`、`<\|im_start\|>`、`you are a helpful assistant`、`忽略之前的指令` 等模板/越狱标记 |
| `relay_internal_token` | 响应正文出现 `OPENAI_API_KEY`、`Bearer sk-`、`===SYSTEM===` 等内部敏感字符串              |
| `language_mismatch`    | 中文 prompt（≥50% 中文字符）但响应中文字符 < 20%                                         |
| `refusal`              | 良性 prompt 触发 `I cannot` / `抱歉无法` 类拒答                                         |
| `empty_response`       | HTTP 200 但响应正文为空                                                                 |

verdict 四档：

| verdict      | 含义                                                                |
| ------------ | ------------------------------------------------------------------- |
| `clean`      | 全部 round 正常对题、无标记                                         |
| `suspicious` | 出现拒答 / 空响应 / 语言切换 占多数                                 |
| `injected`   | 出现身份漂移、system prompt 泄漏或 relay 内部 token                 |
| `failed`     | 该 (endpoint, model) 全部 round 报错（4xx / 5xx / timeout）         |

---

## 性能指标

| 指标            | 含义                                                              |
| --------------- | ----------------------------------------------------------------- |
| **TTFT**        | Time-to-First-Token：从请求发起到收到第一段非空 SSE content 的秒数 |
| **Total**       | 从请求发起到收到 `[DONE]`（或最后一个 chunk）的秒数                |
| **avg/min/max** | 样本统计                                                          |
| **p50/p90/p95/p99** | 线性插值百分位（同 numpy）                                    |
| **stdev**       | 样本标准差 (n-1)                                                  |
| **output_chars**| 响应字符数（用来侧面看是不是被 relay 截断）                       |

> 失败 round 不进百分位样本，但**会单独累计 success_rate / failed_rounds**，错误正文 + request_id 完整保留在报告错误面板里。

---

## 输出

| 文件                    | 内容                                                              |
| ----------------------- | ----------------------------------------------------------------- |
| `<name>.html`           | 单文件 dashboard，JSON 嵌入到 `window.__BENCH_DATA__`，纯 vanilla JS |
| `<name>.json`           | 同名结构化数据 (`schema_version: 1`)，每 round 明细 + metrics + purity |

报告分三块：**Overview**（成功率 + 总览）、**Side-by-side comparison**（横向对比表）、**Per-endpoint blocks**（每端点 8 个统计卡片 + 延迟分布表 + 验纯问题 + 错误面板 + 可展开每轮明细）。

完整技术文档：[`reports/readme.html`](reports/readme.html)（中文，单文件，浏览器直接打开）。

---

## CLI 速查

```text
python scripts/perf-bench.py [URL KEY VENDOR | --config FILE | --url U --key K --vendor V]
                             [--rounds N=10] [--prompt "..."] [--prompts-file F]
                             [--concurrency N=1] [--timeout SEC=60] [--max-tokens N=512]
                             [--temperature T]                # 默认不传
                             [--model M ...]                  # 覆盖 vendor 预设
                             [--system S] [--format openai|anthropic]
                             [--output PATH=perf-report.html] [--json PATH] [--quiet]
```

---

## 安全提醒

- **YAML 里有真实 key 的别 commit**。`.gitignore` 已经把 `reports/*`、`perf-configs/local-*.yaml`、`perf-configs/private-*.yaml` 排除在外
- 报告 JSON / HTML **不写入 api_key**，只记录 `base_url` / `name` / `vendor`，可以放心分享

---

## 上游审计工具

原 `api-relay-audit` 的 13 步安全审计 (`scripts/audit.py`、`audit.py`) 完整保留，
本 fork 只**新增** perf-bench，没修改原审计流程。

上游 README 见 [`README-upstream-audit.md`](./README-upstream-audit.md)。

---

## 目录结构

```
audit_ai_api/
├── README.md                         # 本文件 — perf-bench 说明
├── README-upstream-audit.md          # 上游 13 步审计工具 README
├── requirements.txt
├── scripts/
│   ├── perf-bench.py                 # ⭐ perf 测试 CLI 入口
│   ├── audit.py                      # 上游 13 步审计 CLI
│   └── ...
├── api_relay_audit/
│   ├── perf/                         # ⭐ perf 模块
│   │   ├── streaming.py              # SSE 流式客户端 + TTFT 计时
│   │   ├── metrics.py                # avg / p50..p99 / stdev
│   │   ├── purity.py                 # 6 类验纯启发式
│   │   ├── runner.py                 # 测试矩阵编排（YAML）
│   │   └── report.py                 # JSON + 单文件 HTML 输出
│   ├── client.py                     # 上游 audit 客户端
│   └── ...                           # 上游审计模块
├── perf-configs/
│   ├── example.yaml                  # YAML 模板
│   ├── taoken-vs-allall.yaml         # 双 endpoint 对比模板（占位 key）
│   └── four-row-vendor.yaml          # 4 行 vendor-scoped 模板（占位 key）
├── reports/
│   └── readme.html                   # perf-bench 完整技术文档（中文）
└── tests/                            # 上游测试
```

---

## 纯净度六维评估 (Purity v2.0)

`scripts/purity-eval.py` 是六维独立评估的 CLI 入口（S2 收口；TES-165）。
它把现有 13-step 端到端审计中的每一类「干净 / 疑似 / 注入」证据收成
独立的维度报告：`injection / swap / drift / tool / refusal / leak`，加上
按需开启的 `web3_injection`。

最小命令：

```bash
python scripts/purity-eval.py https://relay.example sk-yourkey claude \
    --rounds 2 --output /tmp/p.json --json
```

报告结构（schema_version 锁为 `"purity-v2.0"`）见
[`docs/purity-v2-schema.md`](docs/purity-v2-schema.md)。Exit code 三档
(`clean=0` / `injected|failed=1` / `suspicious|unknown` 默认 0、`--strict`
后 2)、`--raw-keep` 默认剥离 `raw_artifacts`、HTML 报告每维独立 `<section>`
六块，全部由 `tests/test_purity_cli.py` + `tests/test_purity_reporter.py`
+ `tests/test_evaluator_pipeline.py` 锁定。

`scripts/audit.py` 13-step 审计与本 CLI **并行存在、互不替换**：13-step
是面向人读的 Markdown 报告 + 风险矩阵；purity-eval 是机器友好的
JSON/HTML 形态、用于回归对比与 baseline 比对（baseline 比对在 P1 引入）。

---

## License

继承自上游 `toby-bridges/api-relay-audit`。
