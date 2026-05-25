"""Threshold defaults (design §4.6 last paragraph).

集中阈值定义，便于 wrapper 顶层覆盖。本文件仅定义常量，不读环境变量。
"""

# Refusal verdict 阈值（design §4.6）：
#   refusal_rate ≤ 10% → clean
#   10% < rate ≤ 30%   → suspicious
#   rate > 30%         → injected (over-refusal)
REFUSAL_SUSPICIOUS_RATE: float = 0.10
REFUSAL_INJECTED_RATE: float = 0.30

# rounds_ok 不足判定 unknown 的下限（design §4.6 列 unknown 行 "rounds_ok < rounds_total / 2"）
ROUNDS_OK_UNKNOWN_RATIO: float = 0.5

# hits[].snippet 最大字符数（PRD §4.5 + design §6 AC-N3）
HITS_SNIPPET_MAX_CHARS: int = 200

# JSON --raw-keep=false 大小上限（design §6 AC-N3）
JSON_REPORT_MAX_BYTES: int = 50 * 1024  # 50 KB

# HTML 单文件大小上限（design §6 AC-N2）
HTML_REPORT_MAX_BYTES: int = 500 * 1024  # 500 KB
