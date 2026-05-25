#!/usr/bin/env python3
"""CLI entry point for the perf benchmark extension.

Three invocation styles
-----------------------

1. **Shortest** — three positional arguments ``<url> <key> <vendor>``::

       python scripts/perf-bench.py https://relay.example.com sk-... gpt
       python scripts/perf-bench.py https://relay.example.com sk-... claude

   ``vendor`` picks a representative model preset
   (``gpt`` -> ``gpt-4o-mini``, ``gpt-4o``, ``gpt-5.5``;
   ``claude`` -> ``claude-haiku-4-5``, ``claude-sonnet-4-6``,
   ``claude-opus-4-7``). Useful when one key only works against one vendor.

2. **Single endpoint, flag form** — ``--url`` / ``--key`` (+ optional
   ``--vendor`` / ``--model``)::

       python scripts/perf-bench.py --url https://relay.example.com \
           --key sk-... --vendor claude --rounds 3

3. **Multi-endpoint comparison via YAML**::

       python scripts/perf-bench.py --config perf-configs/multi.yaml \
           --output reports/comparison.html
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Make the sibling module importable when run as ``python scripts/perf-bench.py``
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from api_relay_audit.perf import (load_config, run_benchmark,
                                  write_html_report, write_json_report)
from api_relay_audit.perf.runner import (DEFAULT_CONCURRENCY,
                                         DEFAULT_MAX_TOKENS, DEFAULT_MODELS,
                                         DEFAULT_PROMPT, DEFAULT_ROUNDS,
                                         DEFAULT_TEMPERATURE, DEFAULT_TIMEOUT,
                                         VENDOR_MODELS, vendor_models)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Performance + purity benchmark for AI API relays.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Shortest form (positional): "
            "perf-bench.py <url> <key> <vendor>\n"
            f"Vendors: {', '.join(sorted(VENDOR_MODELS))}"
        ),
    )

    p.add_argument("positional", nargs="*",
                   help="Optional shorthand: <url> <key> <vendor>")

    g_cfg = p.add_argument_group("config (mutually exclusive with positional)")
    g_cfg.add_argument("--config", help="YAML config path")
    g_cfg.add_argument("--url", help="Single endpoint base URL")
    g_cfg.add_argument("--key", help="Single endpoint API key")
    g_cfg.add_argument("--vendor", choices=sorted(VENDOR_MODELS),
                       help="Vendor preset (selects representative models)")
    g_cfg.add_argument("--name", default=None,
                       help="Endpoint label for reports (default: hostname[-vendor])")
    g_cfg.add_argument("--format", choices=["openai", "anthropic"],
                       default="openai",
                       help="Wire format for streaming (default: openai)")

    g_test = p.add_argument_group("test parameters")
    g_test.add_argument("--prompt", default=DEFAULT_PROMPT,
                        help=f"Default test prompt (default: {DEFAULT_PROMPT!r})")
    g_test.add_argument("--prompts-file",
                        help="File with one prompt per line; round i uses line i mod N")
    g_test.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS,
                        help=f"Rounds per (endpoint, model) (default: {DEFAULT_ROUNDS})")
    g_test.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                        help=f"Parallel rounds (default: {DEFAULT_CONCURRENCY})")
    g_test.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Per-request timeout in seconds (default: {DEFAULT_TIMEOUT})")
    g_test.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                        help=f"max_tokens per request (default: {DEFAULT_MAX_TOKENS})")
    g_test.add_argument("--temperature", type=float,
                        default=None,
                        help="Temperature (default: omitted from request body — "
                             "newer models like claude-opus-4-7 reject any value).")
    g_test.add_argument("--model", action="append", default=[],
                        help="Explicit model id (repeatable). "
                             "Overrides --vendor / DEFAULT_MODELS.")
    g_test.add_argument("--system", default=None,
                        help="Optional system prompt")
    g_test.add_argument("--capture-chunk-timings", action="store_true",
                        default=False,
                        help="Capture per-chunk SSE arrival timestamps and "
                             "inter-chunk gaps on every round (off by default; "
                             "raw arrays appear under rounds[].chunk_intervals "
                             "/ chunk_timestamps in the JSON report).")

    g_out = p.add_argument_group("output")
    g_out.add_argument("--output", default="perf-report.html",
                       help="HTML report path (default: perf-report.html)")
    g_out.add_argument("--json", default=None,
                       help="Optional JSON output path. "
                            "Defaults to <output basename>.json next to HTML.")
    g_out.add_argument("--title", default="",
                       help="Override report title")
    g_out.add_argument("--quiet", action="store_true", help="Less stderr noise")

    return p.parse_args()


def _absorb_positional(args: argparse.Namespace) -> None:
    """Map ``perf-bench.py URL KEY [VENDOR]`` onto the ``--url/--key/--vendor`` flags."""
    pos = args.positional or []
    if not pos:
        return
    if args.config:
        sys.exit("error: positional <url> <key> <vendor> is mutually "
                 "exclusive with --config")
    if len(pos) < 2:
        sys.exit("error: positional form needs at least <url> <key>")
    if len(pos) > 3:
        sys.exit("error: positional form is <url> <key> [vendor], extras given")
    url, key, *rest = pos
    if args.url and args.url != url:
        sys.exit("error: positional URL conflicts with --url")
    if args.key and args.key != key:
        sys.exit("error: positional KEY conflicts with --key")
    args.url = args.url or url
    args.key = args.key or key
    if rest:
        v = rest[0].strip().lower()
        if v not in VENDOR_MODELS:
            sys.exit(f"error: unknown vendor {rest[0]!r}; "
                     f"choose from {sorted(VENDOR_MODELS)}")
        if args.vendor and args.vendor != v:
            sys.exit("error: positional vendor conflicts with --vendor")
        args.vendor = v


def main() -> int:
    args = parse_args()
    _absorb_positional(args)

    if args.config and (args.url or args.key):
        sys.exit("error: --config is mutually exclusive with --url/--key")
    if not args.config and not (args.url and args.key):
        sys.exit("error: provide either --config or <url> <key> [vendor] "
                 "(or --url and --key)")

    if args.config:
        cfg = load_config(args.config)
        # CLI overrides win for shared knobs (rounds / prompt / etc.)
        if args.rounds != DEFAULT_ROUNDS:
            cfg["test"]["rounds"] = args.rounds
        if args.timeout != DEFAULT_TIMEOUT:
            cfg["test"]["timeout"] = args.timeout
        if args.prompt != DEFAULT_PROMPT and not cfg["test"].get("prompts"):
            cfg["test"]["prompt"] = args.prompt
        if args.concurrency != DEFAULT_CONCURRENCY:
            cfg["test"]["concurrency"] = args.concurrency
        if args.max_tokens != DEFAULT_MAX_TOKENS:
            cfg["test"]["max_tokens"] = args.max_tokens
        if args.capture_chunk_timings:
            cfg["test"]["capture_chunk_timings"] = True
    else:
        # Resolve the effective model list:
        #   --model overrides everything; otherwise --vendor preset; else default mix.
        if args.model:
            models = list(args.model)
            default_models = list(DEFAULT_MODELS)
        elif args.vendor:
            models = vendor_models(args.vendor)
            default_models = models
        else:
            models = None
            default_models = list(DEFAULT_MODELS)

        ep_name = args.name or _short_host(args.url)
        if args.vendor and not args.name:
            ep_name = f"{ep_name}-{args.vendor}"

        cfg = {
            "test": {
                "prompt": args.prompt,
                "rounds": args.rounds,
                "timeout": args.timeout,
                "max_tokens": args.max_tokens,
                "temperature": args.temperature,
                "concurrency": args.concurrency,
                "format": args.format,
                "system": args.system,
                "prompts": None,
                "capture_chunk_timings": args.capture_chunk_timings,
            },
            "default_models": default_models,
            "endpoints": [{
                "name": ep_name,
                "base_url": args.url,
                "api_key": args.key,
                "models": models,
                "format": args.format,
                "vendor": args.vendor,
            }],
        }

    # Optional prompts file
    if args.prompts_file:
        with open(args.prompts_file, "r", encoding="utf-8") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
        if lines:
            cfg["test"]["prompts"] = lines

    log = (lambda _msg: None) if args.quiet else \
          (lambda msg: print(msg, file=sys.stderr))

    result = run_benchmark(cfg, log_fn=log)

    out_html = args.output
    out_json = args.json or os.path.splitext(out_html)[0] + ".json"

    write_json_report(result, out_json)
    write_html_report(result, out_html, title=args.title)

    if not args.quiet:
        print(f"\nWrote JSON: {out_json}", file=sys.stderr)
        print(f"Wrote HTML: {out_html}", file=sys.stderr)
    return 0


def _short_host(url: str) -> str:
    h = url.split("//", 1)[-1].split("/", 1)[0]
    return h.replace(":", "_")


if __name__ == "__main__":
    sys.exit(main())
