#!/usr/bin/env python3
"""CLI entry — `python scripts/purity-eval.py <target> <api_key> <vendor> [...]`.

S2-4 切片 D：纯度六维评估 CLI。

Flags（design §4.1 + S2-4 plan §2 切片 C/D）:
  positional: target, api_key, vendor {gpt|claude}
  optional:
    --rounds N            每维探测轮数 (default 10)
    --only DIMENSION      仅跑某一维 (mutex with --include)
    --include DIM,DIM     非默认维度并入跑（如 web3_injection）
    --strict              suspicious|unknown 升档为 exit 2
    --baseline PATH       baseline 占位（P1 引入；当前仅打印告警）
    --raw-keep            JSON 保留 raw_artifacts（默认剥离，AC-F5/N3）
    --output PATH         输出文件（按 --json/--html 路由）
    --json                输出 JSON 形式
    --html                输出 HTML 形式（六段，AC-F6/N2）

Exit codes（AC-X1/X2/X3）:
  clean → 0
  injected | failed → 1
  suspicious | unknown 默认 0；--strict 后 2

Stub hook（仅测试使用）:
  环境变量 PURITY_EVAL_STUB_VERDICT 设为 clean/suspicious/injected/failed/unknown
  时，CLI 跳过真实 evaluator pipeline，构造该 verdict 的固定 PurityReportV2，
  用于 tests/test_purity_cli.py。生产路径不依赖此变量。
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api_relay_audit.evaluator.aggregator import (  # noqa: E402
    Overall,
    PurityAggregator,
    PurityReportV2,
)
from api_relay_audit.evaluator.base import (  # noqa: E402
    EvaluatorResult,
    ProbeContext,
)
from api_relay_audit.evaluator.factory import default_purity_pipeline  # noqa: E402
from api_relay_audit.evaluator.reporter import (  # noqa: E402
    render_html,
    render_json,
)


_VALID_DIMENSIONS = (
    "injection",
    "swap",
    "drift",
    "tool",
    "refusal",
    "leak",
    "web3_injection",
)
_DEFAULT_DIMENSIONS = _VALID_DIMENSIONS[:6]


def _compute_exit_code(verdict: str, *, strict: bool) -> int:
    """Map verdict + strict flag → exit code (AC-X1/X2/X3 nucleus, pure fn)."""
    if verdict == "clean":
        return 0
    if verdict in ("injected", "failed"):
        return 1
    if verdict in ("suspicious", "unknown"):
        return 2 if strict else 0
    return 1


def _parse_include(value: str) -> list[str]:
    parts = [p.strip() for p in value.split(",") if p.strip()]
    bad = [p for p in parts if p not in _VALID_DIMENSIONS]
    if bad:
        valid = ", ".join(_VALID_DIMENSIONS)
        raise argparse.ArgumentTypeError(
            f"unknown dimension(s) {bad}; valid choices: {valid}"
        )
    return parts


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="purity-eval",
        description="Purity v2.0 six-dimension evaluator CLI (S2-4).",
    )
    # Positional form (parent issue example): target api_key vendor
    p.add_argument(
        "target_pos",
        nargs="?",
        metavar="target",
        help="Target API base URL (e.g. https://relay.example)",
    )
    p.add_argument(
        "api_key_pos",
        nargs="?",
        metavar="api_key",
        help="API key forwarded to the relay",
    )
    p.add_argument(
        "vendor_pos",
        nargs="?",
        choices=["gpt", "claude"],
        metavar="vendor",
        help="Vendor format (gpt|claude)",
    )
    # Named-flag aliases (parent issue spec): --target / --vendor
    p.add_argument("--target", dest="target_flag", default=None, help="Target API URL")
    p.add_argument("--api-key", dest="api_key_flag", default=None, help="API key")
    p.add_argument(
        "--vendor",
        dest="vendor_flag",
        choices=["gpt", "claude"],
        default=None,
        help="Vendor format",
    )
    p.add_argument("--rounds", type=int, default=10, help="Rounds per dimension")
    p.add_argument(
        "--only",
        choices=_VALID_DIMENSIONS,
        default=None,
        help="Run only this dimension (mutex with --include)",
    )
    p.add_argument(
        "--include",
        type=_parse_include,
        default=None,
        help="Comma-separated non-default dimensions to add (e.g. web3_injection)",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="Promote suspicious|unknown to exit 2 (default 0)",
    )
    p.add_argument(
        "--baseline",
        default=None,
        help="Baseline JSON path (P1 placeholder — currently warns and ignores)",
    )
    p.add_argument(
        "--raw-keep",
        dest="raw_keep",
        action="store_true",
        help="Preserve raw_artifacts in JSON output (default strips them)",
    )
    p.add_argument("--output", default=None, help="Output path")
    p.add_argument("--json", action="store_true", help="Force JSON output")
    p.add_argument("--html", action="store_true", help="Force HTML output")
    p.add_argument(
        "--model",
        default="claude-opus-4-6",
        help="Model id (forwarded to APIClient; not used by stub)",
    )
    return p


def _route_format(args: argparse.Namespace) -> str:
    """Return 'json' or 'html' based on --json/--html or --output suffix."""
    if args.json and args.html:
        raise SystemExit("error: --json and --html are mutually exclusive")
    if args.json:
        return "json"
    if args.html:
        return "html"
    if args.output:
        suffix = Path(args.output).suffix.lower()
        if suffix == ".html":
            return "html"
        if suffix == ".json":
            return "json"
    return "json"


def _maybe_stub_report(
    ctx: ProbeContext, args: argparse.Namespace
) -> Optional[PurityReportV2]:
    """If PURITY_EVAL_STUB_VERDICT is set, return a fixed report (test hook)."""
    verdict = os.environ.get("PURITY_EVAL_STUB_VERDICT")
    if not verdict:
        return None
    if verdict not in ("clean", "suspicious", "injected", "failed", "unknown"):
        return None
    dims = list(_DEFAULT_DIMENSIONS)
    if args.only:
        dims = [args.only]
    elif args.include:
        for extra in args.include:
            if extra not in dims:
                dims.append(extra)
    rounds_total = max(1, args.rounds)
    rounds_ok = rounds_total if verdict == "clean" else 0
    results = [
        EvaluatorResult(
            dimension=d,  # type: ignore[arg-type]
            verdict=verdict,  # type: ignore[arg-type]
            verdict_reason=f"stub: {verdict}",
            rounds_total=rounds_total,
            rounds_ok=rounds_ok,
            hits=[],
            duration_s=0.0,
            raw_artifacts=None,
        )
        for d in dims
    ]
    return PurityAggregator().aggregate(ctx, results)


def _build_real_report(
    ctx: ProbeContext, args: argparse.Namespace
) -> PurityReportV2:
    pipeline = default_purity_pipeline()
    only = args.only if args.only else None
    include = args.include if args.include else None
    return pipeline.run(ctx, only=only, include=include)


def _make_client(args: argparse.Namespace):
    """Lazily import APIClient so test stub path doesn't need httpx."""
    from api_relay_audit.client import APIClient

    return APIClient(
        base_url=args.target,
        api_key=args.api_key,
        model=args.model,
        verbose=False,
    )


def _emit_baseline_warning(args: argparse.Namespace) -> None:
    if args.baseline:
        print(
            "[purity-eval] --baseline is a P1 placeholder; ignoring path",
            file=sys.stderr,
        )


def _write_output(
    report: PurityReportV2,
    *,
    fmt: str,
    raw_keep: bool,
    output: Optional[str],
) -> str:
    if fmt == "html":
        rendered = render_html(report)
    else:
        rendered = render_json(report, raw_keep=raw_keep)
    if output:
        Path(output).write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
        if not rendered.endswith("\n"):
            sys.stdout.write("\n")
    return rendered


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Merge positional + flag forms (flag wins if both supplied).
    target = args.target_flag or args.target_pos
    api_key = args.api_key_flag or args.api_key_pos
    vendor = args.vendor_flag or args.vendor_pos
    if not target or not api_key or not vendor:
        parser.error(
            "missing required argument(s): need target, api_key, vendor "
            "(positional or --target/--api-key/--vendor)"
        )
    args.target = target
    args.api_key = api_key
    args.vendor = vendor

    if args.only and args.include:
        parser.error("--only and --include are mutually exclusive")

    fmt = _route_format(args)
    _emit_baseline_warning(args)

    ctx = ProbeContext(
        client=None,
        api_key=args.api_key,
        base_url=args.target,
        vendor=args.vendor,
        rounds=args.rounds,
        prompts=[],
        sleep_between=0.0,
        keep_raw=args.raw_keep,
    )

    try:
        report = _maybe_stub_report(ctx, args)
        if report is None:
            client = _make_client(args)
            ctx = ProbeContext(
                client=client,
                api_key=args.api_key,
                base_url=args.target,
                vendor=args.vendor,
                rounds=args.rounds,
                prompts=[],
                sleep_between=0.0,
                keep_raw=args.raw_keep,
            )
            report = _build_real_report(ctx, args)
        _write_output(
            report,
            fmt=fmt,
            raw_keep=args.raw_keep,
            output=args.output,
        )
    except KeyboardInterrupt:
        print("\n[purity-eval] interrupted", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"[purity-eval] I/O error: {e}", file=sys.stderr)
        return 1

    return _compute_exit_code(report.overall.verdict, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
