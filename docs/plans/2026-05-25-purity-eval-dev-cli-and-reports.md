# Plan — S2-4 cli-and-reports (TES-165)

> Story: [TES-165] S2-4 Story：CLI 入口 + Reporter + 文档 + 端到端集成测试
> Story 分支: `story/audit-ai-relay/purity-eval/cli-and-reports`
> 合并目标: `epic/audit-ai-relay/purity-eval`
> 启动 SHA: `54f14ac1` (= epic tip)
> 切片粒度: 2-5 分钟/步，零占位符；每步可独立 commit 并 push。

## 0. 边界硬约束（PJM 派单 + 父 issue 描述）

- **只增不改** master 18 个既有测试 + `scripts/audit.py`（13-step 端到端必须保持跑通）
- **不动** `api_relay_audit/reporter.py`（master Markdown reporter，是 dual-distribution diff 参考）
- **不动** `api_relay_audit/evaluator/{base,pipeline,aggregator,factory,baseline,thresholds}.py`（S2-1/2/3 已锁定 schema）
- **schema_version 字面值锁** = `"purity-v2.0"`（design §8 contract）
- **DimensionId 字面值锁** = `injection, swap, drift, tool, refusal, leak, web3_injection`
- CLI exit code：`clean → 0`，`injected | failed → 1`，`suspicious | unknown` 默认 `0`、`--strict` 后 `2`
- AC-R3：两次跑 JSON diff（排除 `metadata.started_at` / `metadata.duration_s`）= 0 行差异

## 1. 改动清单

新增（本 Story 写入）:
- `scripts/purity-eval.py` — CLI 入口（argparse + 子流程）
- `api_relay_audit/evaluator/reporter.py` — JSON v2.0 + HTML 六段渲染
- `tests/test_purity_reporter.py` — 红 → 绿：JSON schema + HTML 段落 + 大小阈值
- `tests/test_purity_cli.py` — 红 → 绿：CLI 参数解析 + exit code + AC-R3 重放幂等
- `tests/test_evaluator_pipeline.py` — 端到端集成：mock honest client → `overall.verdict=clean`
- `docs/purity-v2-schema.md` — JSON schema 文档（AC-D1）
- `docs/plans/2026-05-25-purity-eval-dev-cli-and-reports.md`（本文件）

修改:
- `README.md` — append「纯净度六维评估」章节（AC-D2，单独 commit）

不动:
- `api_relay_audit/reporter.py`、`api_relay_audit/evaluator/{base,pipeline,aggregator,factory,baseline,thresholds}.py`、`api_relay_audit/evaluator/dimensions/*`、`scripts/audit.py`、`web/**`、`.github/workflows/**`、master 18 既有测试

## 2. 切片序列（2-5 分钟粒度）

### 切片 A — fixture + Reporter 红测（约 4 分钟）
1. `tests/test_purity_reporter.py`：写未实现的 `from api_relay_audit.evaluator.reporter import render_json, render_html` 导入 → 红。
2. 用 `PurityAggregator().aggregate(ctx, [])` 构出最小 `PurityReportV2` fixture，断言：
   - `render_json(report, raw_keep=False)` 是 valid JSON、`schema_version == "purity-v2.0"`、`dimensions == {}`、`overall.verdict == "clean"`、`raw_artifacts` 字段不存在（AC-F5）
   - 字节长度 ≤ 50 KB（AC-N3）
3. 再造一份 7 维全 `clean` 的 fixture，跑 `render_html`，断言：
   - 返回 `str`，长度 ≤ 500 KB（AC-N2）
   - 六个默认维度各出现独立 `<section data-dim="..."` 锚点（AC-F6；web3 default-off 不强制）

### 切片 B — Reporter 绿实现（约 5 分钟）
4. `api_relay_audit/evaluator/reporter.py`：
   - `report_to_dict(report, *, raw_keep)` — `PurityReportV2 → dict`，深拷贝 + `raw_keep=False` 时 walk `dimensions[*].raw_artifacts → None`，并将 `None` 字段从输出中剔除（AC-F5 + AC-N3）
   - `render_json(report, *, raw_keep, sort_keys=True, indent=2)` — `json.dumps(..., sort_keys=True, ensure_ascii=False, default=...)`，确保 AC-R3 字段顺序稳定
   - `render_html(report)` — `string.Template` + 内联 CSS，六维各一 `<section data-dim="injection|swap|drift|tool|refusal|leak">`；包含 web3 仅当其在 `dimensions` 内
5. 用纯 `html.escape` 渲染 `hits[].snippet`（防止 fixture 含 `<>` 时 HTML 破裂）
6. 跑切片 A 测试转绿，commit。

### 切片 C — CLI 入口红测（约 4 分钟）
7. `tests/test_purity_cli.py`：用 `subprocess.run([sys.executable, "scripts/purity-eval.py", "--help"])` 断言 exit 0 + 含 `--target/--vendor/--only/--include/--strict/--baseline/--raw-keep/--output/--json/--html` 全部 flag。
8. 用 `monkeypatch` 把 `default_purity_pipeline` 替换为返回固定 `PurityReportV2`（全 `clean`）的 stub，跑：
   - exit code = 0（AC-X1）
   - `--output /tmp/p.json --json` 写出 valid JSON、`overall.verdict=clean`（AC-F1 + AC-F2）
   - `--output /tmp/p.html` 写出 HTML（六段标记齐全）
9. stub 切换返回 `verdict=injected` → exit 1（AC-X2）；`verdict=suspicious` 默认 → exit 0，加 `--strict` → exit 2（AC-X3）

### 切片 D — CLI 绿实现（约 5 分钟）
10. `scripts/purity-eval.py`：
    - `argparse` 定义 11 个 flag（positional `target / api_key / vendor` + `--rounds / --only / --include / --strict / --baseline / --raw-keep / --output / --json / --html`）
    - `--include` 接受逗号分隔 DimensionId 列表，校验合法字面值
    - `--baseline` 仅打印 P1 占位告警，不调用（baseline.py 已 raise）
    - 主流程：构 `APIClient` → `ProbeContext` → `default_purity_pipeline().run(ctx, only=..., include=...)` → `report_to_dict(...)` → 写 `--output`（按后缀路由 JSON / HTML）+ 同时 `--json` / `--html` 强制
    - exit code 表实现见切片 C；写到顶层 `_compute_exit_code(verdict, strict) -> int`，纯函数好测
11. AC-F1 graceful：`try/except` 兜底 `KeyboardInterrupt`、`OSError`（输出路径不可写）→ stderr message + exit 1，不 traceback

### 切片 E — 端到端集成测试（约 5 分钟）
12. `tests/test_evaluator_pipeline.py`：
    - `FakeHonestClient` 类，所有 prompt 返回 `"我是 Claude，由 Anthropic 训练。"` 这类干净响应（用 `api_relay_audit.refusal.CLAUDE_SELF_ID_MARKERS` 命中）；如 dimension 需要 tool/stream/error 子接口，全部返回 honest baseline
    - 调 `default_purity_pipeline().run(ctx)` 跑全 6 默认维度
    - 断言 `report.overall.verdict == "clean"`（AC-F7）
    - 计时断言 ≤ 30s（AC-N1，10 轮）
    - 同一 `FakeHonestClient` 跑两次，`report_to_dict(raw_keep=False)` 排除 `metadata.started_at` / `metadata.duration_s` / `metadata.evaluators_run` 顺序差异后字典深比较相等（AC-R3）

### 切片 F — 文档（约 3 分钟）
13. `docs/purity-v2-schema.md`：列 `purity-v2.0` 顶层字段表、`dimensions[*]` 字段表、`overall` 字段表、verdict 五档语义、AC-F5 raw_artifacts 行为、AC-R3 重放语义、design §8 字面值锁
14. `README.md` append「纯净度六维评估」章节：1 段简介 + 一行最小命令 + 指向 `docs/purity-v2-schema.md`（AC-D2）

### 切片 G — 回归 + 自验 + 评论 + 审查（约 4 分钟）
15. `pytest tests/ -x -q`（master 18 测试 + 本 Story 新增全绿）
16. `python scripts/audit.py --key sk-fake --url https://example.com --model claude-opus-4-6 --skip-infra --skip-context --skip-tool-substitution --skip-error-leakage --skip-stream-integrity --skip-web3-injection --skip-infra-fingerprint --skip-latency-variance` dry-run 不抛（master 13-step 不破坏）
17. `superpowers:verification-before-completion`：收集 stdout + 行号 + `git remote -v` + 当前分支
18. 子 issue 评论：附验证证据 + 关联 plan 切片；`requesting-code-review` 第一轮 @ 资深代码审查专家
19. `git push origin story/audit-ai-relay/purity-eval/cli-and-reports`（增量 push）

## 3. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| 6 维 evaluator 真跑 → 网络耗时 / token 消耗 | `FakeHonestClient` 仅在测试，CLI 仍用真实 `APIClient`；测试不发真实流量 |
| HTML > 500KB | 切片 A AC 测试卡死；内联 CSS 精简、`hits[].snippet` 截断 200 字符 |
| AC-R3 不稳：`metadata.started_at` / `duration_s` 永远不同 | 比对前从 dict 删除这两个字段 + `metadata.evaluators_run` 按维度名排序 |
| `--include` 拼写错误 → argparse error 难懂 | 自定义 `--include` 校验函数，列出合法 DimensionId 字面值 |
| exit code 跨平台差异 | 用 `sys.exit(int)`，不依赖 shell 信号 |
| README 章节冲突 | 在 README 末尾 append，独立 commit；reviewer 易 diff |
| dual-distribution invariant 损坏 | 不动 `api_relay_audit/refusal.py` / `audit.py`；本 Story 仅消费现有 evaluator |

## 4. 验收契约（DoD 自映射）

- AC-F1 graceful exec: 切片 D
- AC-F2 jq schema_version/dimensions/overall.verdict: 切片 C + B
- AC-F5 raw_artifacts == null: 切片 B
- AC-F6 HTML 六段独立: 切片 A + B
- AC-F7 honest client → clean: 切片 E
- AC-X1-3 exit code: 切片 C + D（纯函数 `_compute_exit_code`）
- AC-N1 mock 10 轮 ≤ 30s: 切片 E
- AC-N2 HTML ≤ 500 KB: 切片 A + B
- AC-N3 JSON ≤ 50 KB: 切片 A + B
- AC-D1 docs/purity-v2-schema.md: 切片 F
- AC-D2 README 章节: 切片 F
- AC-R3 重放幂等: 切片 E
- master 18 测试 + 13-step 不破坏: 切片 G
- 两轮 reviewer LGTM: 切片 G 起（合并目标 = epic 分支）
- QA 局部 + S2 集成验收: reviewer 后由 QA 介入
- 合并到 `epic/audit-ai-relay/purity-eval`（门 4）: PJM 协调

## 5. 时间线（粗估）

- 切片 A-G 单独可独立 commit；累计 ~30 分钟编码 + 测试，~10 分钟自验/审查/push
- 第一轮 reviewer 评论 → 修缺陷预留 20 分钟
- QA 局部验收预留 15 分钟
- 总预算 75 分钟，~1 小时窗口落地
