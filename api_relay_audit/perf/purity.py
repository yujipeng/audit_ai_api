"""Lightweight purity / prompt-injection check that re-uses perf responses.

The audit framework already ships a 13-step deep audit. The goal here is
to flag obvious anomalies in the *perf benchmark corpus itself* so the
HTML report can show a "purity" score next to the latency numbers.

Signals detected:

* **Identity leakage** — response advertises a non-Claude / non-OpenAI
  identity that contradicts the requested model (re-uses
  :mod:`api_relay_audit.identity_patterns`).
* **System-prompt leakage** — telltale phrases like ``you are a helpful
  assistant``, ``system:`` / ``SYSTEM:`` headers, or hidden-prompt
  artefacts that the relay might inject.
* **Empty / refusal** — response is empty, explicitly refuses, or is
  off-topic boilerplate.
* **Language mismatch** — Chinese prompt, response is mostly English
  (or vice-versa) past a tiny intro.
* **Suspicious markers** — known relay-injection sentinels (``[INST]``,
  raw template tokens, ``<|im_start|>``, etc.).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Optional

from api_relay_audit.identity_patterns import find_non_claude_identities


# -- Heuristic patterns ------------------------------------------------------

SYSTEM_LEAK_PATTERNS = [
    re.compile(r"you are (?:a|an) (?:helpful|harmless|honest)", re.I),
    re.compile(r"\bsystem\s*[:：]\s*you", re.I),
    re.compile(r"<\|(?:im_start|im_end|system|user|assistant)\|>", re.I),
    re.compile(r"\[INST\]|\[/INST\]"),
    re.compile(r"<\|begin_of_text\|>|<\|end_of_text\|>"),
    re.compile(r"忽略.{0,15}(?:之前|上面|以上).{0,15}指令", re.I),
    re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior)\s+instructions", re.I),
]

REFUSAL_PATTERNS = [
    re.compile(r"\b(?:i\s*can(?:not|'t)|i\s*am\s*unable\s*to|i\s*won'?t)\b", re.I),
    re.compile(r"抱歉.{0,10}(?:无法|不能|不可以)", re.I),
    re.compile(r"对不起.{0,10}(?:无法|不能)", re.I),
    re.compile(r"我没有(?:相关|这方面)?(?:的)?(?:能力|信息)", re.I),
]

# Tokens that should never appear in a relayed response — they'd indicate
# the relay is leaking its upstream payload.
LEAKAGE_TOKENS = [
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "Bearer sk-",
    "x-api-key", "internal_only", "DEBUG_PROMPT",
    "===SYSTEM===", "###SYSTEM###",
]


@dataclass
class PurityRecord:
    """Result of analysing one model response for one perf round."""

    round_index: int
    ok: bool
    text_preview: str
    issues: list[str] = field(default_factory=list)
    identities: list[str] = field(default_factory=list)
    refusal: bool = False
    empty: bool = False
    language_mismatch: bool = False


@dataclass
class PuritySummary:
    """Aggregate purity outcome for one (endpoint, model) bench."""

    rounds_total: int
    rounds_ok: int
    rounds_clean: int
    issue_counts: dict[str, int] = field(default_factory=dict)
    identities_seen: dict[str, int] = field(default_factory=dict)
    refusal_count: int = 0
    empty_count: int = 0
    language_mismatch_count: int = 0
    verdict: str = "unknown"  # clean | suspicious | injected | failed
    verdict_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "rounds_total": self.rounds_total,
            "rounds_ok": self.rounds_ok,
            "rounds_clean": self.rounds_clean,
            "issue_counts": dict(self.issue_counts),
            "identities_seen": dict(self.identities_seen),
            "refusal_count": self.refusal_count,
            "empty_count": self.empty_count,
            "language_mismatch_count": self.language_mismatch_count,
            "verdict": self.verdict,
            "verdict_reason": self.verdict_reason,
        }


# -- Language detection ------------------------------------------------------

def _ratio_chinese(text: str) -> float:
    if not text:
        return 0.0
    cn = 0
    total = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if "CJK" in unicodedata.name(ch, ""):
            cn += 1
    return cn / total if total else 0.0


def _ratio_chinese_in_prompt(prompt: str) -> float:
    return _ratio_chinese(prompt)


# -- Per-response analysis ---------------------------------------------------

def analyze_response(text: str, *, prompt: str, model: str,
                     ok: bool, round_index: int) -> PurityRecord:
    """Examine one streamed response and return a :class:`PurityRecord`."""
    rec = PurityRecord(
        round_index=round_index,
        ok=ok,
        text_preview=(text or "")[:400],
    )
    if not ok:
        rec.issues.append("upstream_error")
        return rec
    if not text or not text.strip():
        rec.empty = True
        rec.issues.append("empty_response")
        return rec

    # Identity leakage — only flag if the keyword survived the
    # word-boundary check from identity_patterns.
    identities = find_non_claude_identities(text)
    if identities:
        # Suppress noise: many models legitimately mention "GPT" or
        # "Claude" when answering the user's question. Only count it
        # as identity leakage when the response *claims* to be that
        # model (i.e. an "I am X" / "我是X" anchor exists).
        anchored = [
            kw for kw in identities
            if _identity_anchored(text, kw)
        ]
        if anchored:
            rec.identities = anchored
            rec.issues.append("identity_leak")

    # System-prompt leakage / injection markers
    for pat in SYSTEM_LEAK_PATTERNS:
        if pat.search(text):
            rec.issues.append("system_prompt_leak")
            break
    for token in LEAKAGE_TOKENS:
        if token in text:
            rec.issues.append("relay_internal_token")
            break

    # Refusals (expected to be rare for a benign cuisine prompt)
    for pat in REFUSAL_PATTERNS:
        if pat.search(text):
            rec.refusal = True
            rec.issues.append("refusal")
            break

    # Language mismatch — Chinese prompt should yield mostly Chinese answer
    if _ratio_chinese_in_prompt(prompt) >= 0.5:
        if len(text) >= 60 and _ratio_chinese(text) < 0.2:
            rec.language_mismatch = True
            rec.issues.append("language_mismatch")

    return rec


_IDENTITY_ANCHORS = [
    r"\bi\s+am\b", r"\bi'?m\b", r"\bmy\s+name\s+is\b",
    r"\bcalled\b", r"i\s+was\s+made\s+by",
    r"我是", r"本人是", r"我叫", r"我的名字是", r"我由",
]
_IDENTITY_ANCHOR_RX = re.compile("|".join(_IDENTITY_ANCHORS), re.I)


def _identity_anchored(text: str, keyword: str) -> bool:
    """Return True iff an identity-claim anchor occurs near ``keyword``."""
    # Walk every occurrence (case-insensitive) and check a 60-char window
    # before the keyword for an "I am X" / "我是" style claim.
    lower = text.lower()
    kw = keyword.lower()
    idx = 0
    while True:
        pos = lower.find(kw, idx)
        if pos < 0:
            return False
        window = text[max(0, pos - 80): pos + len(keyword)]
        if _IDENTITY_ANCHOR_RX.search(window):
            return True
        idx = pos + len(keyword)


# -- Aggregate summary -------------------------------------------------------

def analyze_purity(records: Iterable[PurityRecord]) -> PuritySummary:
    records = list(records)
    total = len(records)
    ok = sum(1 for r in records if r.ok)
    clean = sum(1 for r in records if r.ok and not r.issues)

    issue_counts: dict[str, int] = {}
    identities_seen: dict[str, int] = {}
    refusal = empty = lang = 0
    for r in records:
        for iss in r.issues:
            issue_counts[iss] = issue_counts.get(iss, 0) + 1
        for kw in r.identities:
            identities_seen[kw] = identities_seen.get(kw, 0) + 1
        if r.refusal:
            refusal += 1
        if r.empty:
            empty += 1
        if r.language_mismatch:
            lang += 1

    summary = PuritySummary(
        rounds_total=total,
        rounds_ok=ok,
        rounds_clean=clean,
        issue_counts=issue_counts,
        identities_seen=identities_seen,
        refusal_count=refusal,
        empty_count=empty,
        language_mismatch_count=lang,
    )

    summary.verdict, summary.verdict_reason = _verdict(summary)
    return summary


def _verdict(s: PuritySummary) -> tuple[str, str]:
    if s.rounds_total == 0:
        return "failed", "no rounds"
    if s.rounds_ok == 0:
        return "failed", "every round errored upstream"
    if s.issue_counts.get("identity_leak"):
        kws = sorted(s.identities_seen.keys())
        return ("injected",
                f"identity leak detected ({', '.join(kws)})")
    if s.issue_counts.get("system_prompt_leak"):
        return "injected", "system prompt / template tokens leaked"
    if s.issue_counts.get("relay_internal_token"):
        return "injected", "relay-internal token found in response"
    if s.issue_counts.get("language_mismatch", 0) > s.rounds_ok * 0.5:
        return ("suspicious",
                "majority of responses ignored the prompt language")
    if s.issue_counts.get("refusal", 0) > s.rounds_ok * 0.5:
        return "suspicious", "majority refusals on a benign prompt"
    if s.empty_count > s.rounds_ok * 0.3:
        return "suspicious", "frequent empty responses"
    if s.rounds_ok < s.rounds_total * 0.5:
        return ("suspicious",
                f"only {s.rounds_ok}/{s.rounds_total} rounds succeeded")
    if s.rounds_clean == s.rounds_ok and s.rounds_ok == s.rounds_total:
        return "clean", "all rounds responded coherently with no markers"
    return ("clean",
            f"{s.rounds_clean}/{s.rounds_total} clean rounds, no injection markers")
