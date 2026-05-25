"""CI credential grep gate — invoked by .github/workflows/credential-scan.yml.

Walks `git ls-files`, applies the same three pattern families that drive
security/redact.py, and exits non-zero on any match outside an allow-listed
set of files (the redact module itself + its tests + this script).

Synthetic placeholders (`sk-REPLACE_ME_...`, `sk-test-...`, `sk-fake-...`,
all-same-char fillers, etc.) are detected and ignored: only credential-shaped
strings with realistic entropy trip the gate. The placeholder heuristic
intentionally lives here, not in security/redact.py, because production
redaction must be conservative ("if it looks like a key, redact it") while
CI gating must be precise ("only fail on values that could be real keys").

Kept as a standalone script so the workflow YAML stays free of inline regex
strings (no GitHub-Actions-injection surface, no double-escaping).
"""

from __future__ import annotations

import math
import re
import subprocess
import sys
from pathlib import Path

ALLOWLIST = {
    "security/redact.py",
    "security/secrets.py",
    "tests/test_redact_patterns.py",
    "tests/test_secrets_loader.py",
    "tests/test_credential_isolation.py",
    "tests/test_credential_isolation_orchestrator.py",
    "tests/test_orchestration_config.py",
    "tests/test_orchestration_runner.py",
    "tests/test_step_adapters.py",
    # The error-leakage detector tests intentionally embed leaked-credential
    # fixtures (the file is the corpus for testing how relays leak keys).
    "tests/test_error_leakage.py",
    "scripts/credential_scan.py",
    ".github/workflows/credential-scan.yml",
}

PATTERNS = [
    ("openai-style-key", re.compile(r"sk-[A-Za-z0-9_\-]{20,}")),
    ("bearer-token", re.compile(r"(?i)Bearer\s+[A-Za-z0-9._\-]{20,}")),
    (
        "header-or-config-key",
        re.compile(
            r"(?i)(?:x[-_]?)?(?:api[_-]?key|access[_-]?token|secret[_-]?key)"
            r"\s*[:=]\s*[\"']?[A-Za-z0-9._\-]{20,}"
        ),
    ),
]

_PLACEHOLDER_TOKENS = (
    "replace_me",
    "placeholder",
    "example",
    "dummy",
    "fake",
    "fill_me",
    "nothing-real",
    "nothing-fake",
    "nothing_real",
    "nothing_fake",
    "your-",
    "your_",
    "xxxx",
    "<key>",
    "-test-",
    "-fake-",
    "-other-",
    "-demo-",
    "-example-",
    "-sample-",
)


def _looks_synthetic(match_text: str) -> bool:
    """Heuristic: is this credential-shaped string obviously synthetic?

    A real API key is high-entropy and free of self-describing markers. We
    treat the match as synthetic if any of:
      * contains an explicit placeholder token (`REPLACE_ME`, `-test-`, etc.)
      * has low Shannon entropy on its secret-body (e.g. `sk-aaaaaaaaa...`)
    """
    lowered = match_text.lower()
    if any(tok in lowered for tok in _PLACEHOLDER_TOKENS):
        return True

    # Pull the credential body — strip a leading label/prefix if present.
    body = match_text
    for prefix in ("sk-", "Bearer ", "bearer "):
        if body.startswith(prefix):
            body = body[len(prefix) :]
            break

    # Shannon entropy on the body (over alphabet of present chars). Real keys
    # are uniformly random base64-ish; entropy is bounded below by ~4 bits/char.
    if len(body) < 8:
        return False
    counts: dict[str, int] = {}
    for ch in body:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(body)
    entropy = -sum((c / n) * math.log2(c / n) for c in counts.values())
    # Threshold: 2.8 bits/char. `sk-aaaaaaa...` ~ 0 bits/char, real key > 4.
    return entropy < 2.8


def _tracked_files() -> list[str]:
    out = subprocess.check_output(["git", "ls-files"], text=True)
    return [line for line in out.splitlines() if line]


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    hits: list[tuple[str, str, int, str]] = []
    for rel in _tracked_files():
        if rel in ALLOWLIST:
            continue
        path = repo_root / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for pattern_name, regex in PATTERNS:
            for m in regex.finditer(text):
                if _looks_synthetic(m.group(0)):
                    continue
                line_no = text.count("\n", 0, m.start()) + 1
                line_text = text.splitlines()[line_no - 1].strip()[:200]
                hits.append((pattern_name, rel, line_no, line_text))

    if hits:
        print("credential-scan: matches found in tracked files.")
        print("Move secrets to env and reference them as $ENV{VAR_NAME}.")
        print()
        for pattern_name, rel, line_no, snippet in hits:
            print(f"  [{pattern_name}] {rel}:{line_no}: {snippet}")
        return 1

    for rel in (
        "security/redact.py",
        "security/secrets.py",
        "tests/test_redact_patterns.py",
        "tests/test_credential_isolation.py",
    ):
        if not (repo_root / rel).is_file():
            print(f"credential-scan: allow-listed file missing: {rel}")
            return 1

    print("credential-scan: 0 matches in tracked files (allow-list excluded).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
