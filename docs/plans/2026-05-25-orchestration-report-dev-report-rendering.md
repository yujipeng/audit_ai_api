# Plan — S5-C report-rendering (TES-129)

> Story: [TES-129] S5-C 报告渲染 + baseline diff
> Story 分支: `story/audit-ai-relay/orchestration-report/report-rendering`
> 合并目标: `epic/audit-ai-relay/orchestration-report`
> 启动 SHA: `03549d2b` (= epic tip)
> 切片粒度: 2-5 分钟/步，零占位符；每步可独立 commit 并 push。

## 0. 边界硬约束（PJM 收敛版，2026-05-25 项目负责人裁决）

- **只读消费** S5-B 的 `run-record.json` schema v1；运行时 `schema_version != 1` → fail-fast。
- **schema 灵感来源 `web/data-example.json`@5d8eedc3**（已查阅）；运行时**不读 `web/`**，离线 HTML 自包含、可断网。
- **不动 `web/` / `.github/workflows/` / `audit.py` / `audit_ai_api/reporter.py`**；`api_relay_audit/reporter.py` 是 golden-file diff 参考，不可改。
- redact 走 S5-A `security.redact` / `security.bearer`，不允许 inline 实现。
- 三路渲染（MD/HTML/JSON）必须从**同一个 `run-record.json`** 提取数据，cell coverage 一致。

## 1. 改动清单

新增（本 Story 写入）:
- `reporting/__init__.py`                — 包初始化 + 公共导出
- `reporting/schema.py`                  — 内部 view-model schema（受 `web/data-example.json` 启发，**仅设计参考**）
- `reporting/render_markdown.py`         — Markdown 渲染（消费 `run-record.json`）
- `reporting/render_html.py`             — 单文件 HTML 渲染（≤ 2 MB 自包含）
- `orchestration/baseline.py`            — 当前 run vs 上次基线 diff
- `tests/test_report_rendering.py`       — 红 → 绿 测试集
- `tests/golden/report-rendering/run-record.fixture.json` — 三路一致性 + golden-file diff 的源数据 fixture
- `tests/golden/report-rendering/expected.md`            — golden Markdown 输出
- `docs/plans/2026-05-25-orchestration-report-dev-report-rendering.md` (本文件)

不动:
- `web/**`、`.github/workflows/**`、`audit.py`、`api_relay_audit/reporter.py`、`orchestration/run_record.py`（schema 锁定）

## 2. 切片序列（2-5 分钟粒度）

### 切片 A — 准备 fixture + 测试骨架（约 4 分钟）
1. 在 `tests/golden/report-rendering/` 落 `run-record.fixture.json`：
   - 4 个 step × 2 个 endpoint × 1 个 model = 8 cells
   - 至少 1 个 cell `status=error`、1 个 `cache_hit=true`、1 个 `payload` 含 `score/latency_ms/note` 三段
   - `redacted_key_ids` 含 2 个不同 id
2. 在 `tests/test_report_rendering.py` 写空骨架（导入未完成的 `reporting.*` + `orchestration.baseline`）→ 红。

### 切片 B — schema 内部 view-model（约 5 分钟）
3. `reporting/schema.py`：定义内部 dataclass `ReportView`，字段映射来源在 docstring 标注「灵感来源 `web/data-example.json`@5d8eedc3」：
   - 顶层: `generated_at`、`tool`、`code_version`、`elapsed_seconds`、`config_digest`、`redacted_key_ids`、`summary`、`cells`（按 step 分组）
   - 每 cell 视图: `step / endpoint / model / status / latency_ms / cache_hit / payload_excerpt / error_excerpt`
4. 提供 `ReportView.from_record(record: dict) -> ReportView`，含 `schema_version != 1 → ValueError` 的 fail-fast。
5. 添加 `RED-only` 单测验证 fail-fast。

### 切片 C — render_markdown（约 5 分钟）
6. `reporting/render_markdown.py`：`render(record) -> str`；输出结构：
   - 标题块（title + Generated + Tool + Config digest + Elapsed）
   - Summary block（total / ok / error / cache_hits / hit_rate）
   - Per-step section（h2 step，h3 endpoint，bullet status/latency/cache_hit/payload-summary）
   - Error block（汇总 status=error 单元）
7. 字体 / icon / emoji 套用 `api_relay_audit/reporter.py` flag icon 表（🔴/🟡/🟢，确保 dual-distribution 风格一致）。
8. 生成 `tests/golden/report-rendering/expected.md`：从 fixture 渲染一次，落盘；写 golden-file diff 测试。

### 切片 D — render_html（约 5 分钟）
9. `reporting/render_html.py`：`render(record) -> str`；模板用 `string.Template` + 内联 CSS / JS（≤ 2 MB 自包含）：
   - 顶部 summary 卡片
   - 四维度 sections（probe / purity / perf / pricing）
   - 内嵌 sort / filter via 纯 vanilla JS（无外链）
   - 内嵌 diff 视图占位区（baseline 接入点）
10. AC 测试: `len(html.encode("utf-8")) <= 2 * 1024 * 1024`、断言 `'http://' not in html` + `'https://' not in html` + `'<link rel="stylesheet"' not in html`。

### 切片 E — baseline diff（约 4 分钟）
11. `orchestration/baseline.py`：`load_baseline(path) -> dict`、`compute_diff(current: dict, baseline: dict) -> dict`：
   - perf 维度: latency_ms 相对变化 ≥ 10% → flag
   - purity / pricing / probe 维度: payload.score 绝对下降 ≥ 0.1 → flag
   - `cache_hit` 字段显式标注（design §2.4 R9）：缓存命中的 cell 不参与 perf 阈值判断
   - 输出 `diff.md` 顶部 summary（"N flagged, M unchanged"）
12. 三路一致性测试 + baseline 阈值测试（含 cache_hit 例外路径）。

### 切片 F — 凭证回归 + sentinel fuzz（约 3 分钟）
13. 扩展 `tests/test_credential_isolation.py` 类 `TestRule4_SentinelFuzz`：让 fuzz 通过新的 `reporting.render_markdown.render` / `reporting.render_html.render` / `orchestration.baseline.compute_diff` 三个真实管道（不再仅模拟），断言 sentinel 0 命中。
14. CI 配置：本测试已被 `pytest tests/` 默认收集，无需新增 hook；在 plan 切片中标注 reviewer 关注点（端到端而非 unit）。

### 切片 G — 自验 / 评论 / 第一轮审查（约 3 分钟）
15. 跑 `pytest tests/test_report_rendering.py tests/test_credential_isolation.py tests/test_run_record_schema.py -v`。
16. `superpowers:verification-before-completion`：收集证据 + 行号 + remote/branch stdout。
17. 子 issue 评论：附验证证据 + 引用本 plan 切片；`requesting-code-review` 第一轮 @ 资深代码审查专家。

## 3. 风险与缓解

| 风险 | 缓解 |
| --- | --- |
| HTML > 2 MB | 切片 D AC 测试卡死；CSS/JS 精简、payload 截断 1k 字符 |
| golden MD 频繁失败（noise） | 不在渲染中嵌入 wallclock；`generated_at` 改读 fixture 字段而非 `datetime.now` |
| sentinel fuzz 走 reporting/* 后泄漏 | 所有 `payload_excerpt` / `error_excerpt` 走 `redact_response_text(text, sentinel_keys)`，sentinel 集合从 `redacted_key_ids` 衍生 |
| baseline diff cache_hit 边界 | 显式 `if cell.cache_hit: skip perf threshold; still emit purity threshold` |
| schema_version != 1 → 用户体验差 | fail-fast 抛 `ValueError("schema_version=2 not supported; consumer is pinned to v1")` |

## 4. 验收契约（DoD 自映射）

- AC-S5-006 baseline diff: 切片 E
- AC-S5-007 HTML 报告: 切片 D
- Golden-file diff: 切片 C
- 三路一致性: 切片 C + D（共用 fixture，cell coverage 校验）
- schema_version=1 fail-fast: 切片 B
- HTML 结构契约 web/ owner 三方握手记录: PJM 评论 [98a7ca27](mention://issue/12a2a515-7890-4408-a6f1-bc2cda24b722) 已落档
- 凭证回归 sentinel-fuzz HTML/MD/diff 0 命中: 切片 F
- 两轮 reviewer LGTM: 切片 G 起
- QA 局部六维: reviewer 后由 QA 介入
- 合并到 epic 分支（门 4）: PJM 协调
