#!/usr/bin/env bash
# 三合一检查：性能 + 纯净度 + 13 步安全审计
#
# 用法:
#   scripts/full-check.sh <name> <url> <key> <vendor> [model] [profile]
#
# 参数:
#   name     报告目录名前缀，例如 relay-A
#   url      relay 的 base URL
#   key      该通路的 API key
#   vendor   claude | gpt
#   model    可选。13 步审计用的具体模型。
#            缺省时：claude → claude-sonnet-4-6，gpt → gpt-5.3-codex
#   profile  可选。安全审计 profile：general | web3 | full（默认 general）
#
# 输出:
#   reports/<name>-<YYYYMMDD-HHMM>/
#       perf.html / perf.json   性能 + 纯净度
#       audit.md                13 步安全审计

set -euo pipefail

if [ $# -lt 4 ]; then
    sed -n '2,17p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
fi

NAME=$1
URL=$2
KEY=$3
VENDOR=$4
MODEL=${5:-$([ "$VENDOR" = "claude" ] && echo "claude-sonnet-4-6" || echo "gpt-5.3-codex")}
PROFILE=${6:-general}

TS=$(date +%Y%m%d-%H%M)
OUT="reports/${NAME}-${TS}"
mkdir -p "$OUT"

echo "==> [1/2] perf + purity  (vendor=$VENDOR, 10 rounds)"
python scripts/perf-bench.py "$URL" "$KEY" "$VENDOR" \
    --rounds 10 \
    --output "$OUT/perf.html"

echo
echo "==> [2/2] 13-step security audit  (model=$MODEL, profile=$PROFILE)"
python scripts/audit.py \
    --key "$KEY" \
    --url "$URL" \
    --model "$MODEL" \
    --profile "$PROFILE" \
    --output "$OUT/audit.md"

echo
echo "==> Done. 报告目录: $OUT"
ls -lh "$OUT"
