"""Tokenizer version fingerprint (Story TES-135 / S4-B, R5 落地点 2).

R5 落地点 2 in plain English: the L1 tokenizer's "ground truth" depends on
the exact ``tiktoken`` / ``anthropic`` versions installed on the runner. If
those bump silently, today's L1 verdict is no longer comparable with
yesterday's. To catch silent drift, CI calls
:func:`check_tokenizer_fingerprint` on startup; mismatches surface as a
**WARN** (not a hard fail — precision degrades gradually, a hard fail would
be too noisy and unactionable in the moment).

The pinned baseline lives in ``tokenizer_fingerprint.yaml`` next to this
file. Bumping it should be a deliberate audit-rebaseline action.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Optional


_BASELINE_PATH = Path(__file__).parent / "tokenizer_fingerprint.yaml"


class FingerprintStatus(str, Enum):
    OK = "ok"
    WARN = "warn"
    SKIP = "skip"


@dataclass
class TokenizerFingerprintResult:
    status: FingerprintStatus
    message: str
    expected: dict[str, str]
    observed: dict[str, Optional[str]]


def _load_fingerprint_yaml() -> dict[str, str]:
    """Read the pinned baseline from disk. Tests monkeypatch this with a
    synthetic dict so they can exercise WARN/SKIP/OK transitions without
    rewriting the on-disk YAML."""
    import yaml

    if not _BASELINE_PATH.exists():
        return {}
    raw = yaml.safe_load(_BASELINE_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def _installed_version(name: str) -> Optional[str]:
    """Return the installed package version, or ``None`` if absent."""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover — Py 3.8+
        return None
    try:
        return version(name)
    except PackageNotFoundError:
        return None
    except Exception:
        return None


def check_tokenizer_fingerprint() -> TokenizerFingerprintResult:
    """Compare installed tokenizer-related packages against the pinned
    baseline. Soft gate — never raises.

    Returns:
      - **OK**: all pinned packages installed AND versions match.
      - **WARN**: at least one package installed but version differs.
      - **SKIP**: none of the pinned packages are installed (the default
        zero-dep install path; expected for users who never opted in).
    """
    expected = _load_fingerprint_yaml()
    observed: dict[str, Optional[str]] = {
        name: _installed_version(name) for name in expected
    }

    if not expected:
        return TokenizerFingerprintResult(
            status=FingerprintStatus.SKIP,
            message="No tokenizer fingerprint baseline configured.",
            expected=expected,
            observed=observed,
        )

    if all(v is None for v in observed.values()):
        return TokenizerFingerprintResult(
            status=FingerprintStatus.SKIP,
            message=(
                "tokenizer-meter packages not installed (default install / "
                "zero-dep path). L1 tokenizer audit is disabled. "
                "Install audit_ai_api[token-meter] to enable L1."
            ),
            expected=expected,
            observed=observed,
        )

    drifts: list[str] = []
    for name, want in expected.items():
        got = observed.get(name)
        if got is None:
            continue  # absent: don't WARN, that's a SKIP-eligible state
        if got != want:
            drifts.append(f"{name}: expected {want}, observed {got}")

    if drifts:
        return TokenizerFingerprintResult(
            status=FingerprintStatus.WARN,
            message=(
                "Tokenizer fingerprint drift (soft gate, audit continues): "
                + "; ".join(drifts)
                + ". L1 verdicts may not be comparable across this drift; "
                "consider rebaselining tokenizer_fingerprint.yaml."
            ),
            expected=expected,
            observed=observed,
        )

    return TokenizerFingerprintResult(
        status=FingerprintStatus.OK,
        message="Tokenizer fingerprint OK.",
        expected=expected,
        observed=observed,
    )


__all__ = [
    "FingerprintStatus",
    "TokenizerFingerprintResult",
    "check_tokenizer_fingerprint",
]
