"""Tokenizer version fingerprint (Story TES-135 / S4-B, R5 落地点 2).

Why this exists: the L1 evaluator's "ground truth" is whatever tokenizer
shipped with the installed ``tiktoken``/``anthropic`` packages on the runner.
If those packages bump majors silently, an L1 verdict computed yesterday is
no longer apples-to-apples with one computed today. To catch this we pin a
fingerprint (package name → expected version) in
``api_relay_audit/pricing/tokenizer_fingerprint.yaml`` and verify it at CI
startup. Mismatches are surfaced as a WARN — they do NOT fail the build
(precision drifts gradually; a hard failure would be too noisy), but they
DO appear in the CI log and in the audit report so reviewers can decide.

Contract:
  - ``check_tokenizer_fingerprint()`` returns ``TokenizerFingerprintResult``
    with ``.status`` in {OK, WARN, SKIP}.
  - ``WARN`` only when the package IS installed and its version differs
    from the pinned baseline.
  - ``SKIP`` when the package is absent (the default-install path — this is
    the correct, expected state for users who never opted into extras).
"""

from __future__ import annotations

import textwrap

import pytest


def _make_module(monkeypatch, fingerprint_yaml: str):
    """Reload the fingerprint module with a synthetic YAML payload."""
    import importlib
    import sys

    from api_relay_audit.pricing import tokenizer_fingerprint as tf_mod
    importlib.reload(tf_mod)
    monkeypatch.setattr(
        tf_mod,
        "_load_fingerprint_yaml",
        lambda: __import__("yaml").safe_load(fingerprint_yaml),
    )
    return tf_mod


class TestFingerprintModuleSurface:
    def test_module_imports_in_default_environment(self):
        """No `pricing.tokenizer_fingerprint` raise on plain import."""
        from api_relay_audit.pricing import tokenizer_fingerprint  # noqa: F401

    def test_check_returns_status_enum(self):
        from api_relay_audit.pricing.tokenizer_fingerprint import (
            check_tokenizer_fingerprint,
            FingerprintStatus,
        )
        result = check_tokenizer_fingerprint()
        assert result.status in (
            FingerprintStatus.OK,
            FingerprintStatus.WARN,
            FingerprintStatus.SKIP,
        )


class TestFingerprintBehavior:
    def test_skip_when_package_absent(self, monkeypatch):
        from api_relay_audit.pricing.tokenizer_fingerprint import (
            FingerprintStatus,
        )

        tf_mod = _make_module(
            monkeypatch,
            textwrap.dedent(
                """
                tiktoken: "9.99.99"
                anthropic: "9.99.99"
                """
            ),
        )

        # Force "tiktoken/anthropic absent" by stubbing the version lookup.
        monkeypatch.setattr(tf_mod, "_installed_version", lambda name: None)

        result = tf_mod.check_tokenizer_fingerprint()
        assert result.status is FingerprintStatus.SKIP
        # SKIP must explicitly mention the default-install zero-dep path so
        # reviewers don't misread a SKIP as a silent miss.
        assert "default install" in result.message.lower() or \
               "not installed" in result.message.lower()

    def test_ok_when_versions_match(self, monkeypatch):
        from api_relay_audit.pricing.tokenizer_fingerprint import (
            FingerprintStatus,
        )

        tf_mod = _make_module(
            monkeypatch,
            textwrap.dedent(
                """
                tiktoken: "0.13.0"
                anthropic: "0.39.0"
                """
            ),
        )
        monkeypatch.setattr(
            tf_mod,
            "_installed_version",
            lambda name: {"tiktoken": "0.13.0", "anthropic": "0.39.0"}.get(name),
        )

        result = tf_mod.check_tokenizer_fingerprint()
        assert result.status is FingerprintStatus.OK

    def test_warn_when_version_drifts(self, monkeypatch):
        from api_relay_audit.pricing.tokenizer_fingerprint import (
            FingerprintStatus,
        )

        tf_mod = _make_module(
            monkeypatch,
            textwrap.dedent(
                """
                tiktoken: "0.13.0"
                anthropic: "0.39.0"
                """
            ),
        )
        monkeypatch.setattr(
            tf_mod,
            "_installed_version",
            lambda name: {"tiktoken": "0.14.5", "anthropic": "0.39.0"}.get(name),
        )

        result = tf_mod.check_tokenizer_fingerprint()
        assert result.status is FingerprintStatus.WARN
        # WARN must name BOTH the expected and the observed version so a
        # reviewer can correlate against history.
        assert "0.13.0" in result.message
        assert "0.14.5" in result.message
        assert "tiktoken" in result.message

    def test_warn_does_not_raise(self, monkeypatch):
        """The fingerprint check is a soft gate — it must never throw,
        regardless of the situation (so CI doesn't fail on it)."""
        tf_mod = _make_module(
            monkeypatch,
            textwrap.dedent(
                """
                tiktoken: "0.13.0"
                """
            ),
        )
        monkeypatch.setattr(
            tf_mod,
            "_installed_version",
            lambda name: "99.99.99" if name == "tiktoken" else None,
        )
        # Must not raise.
        tf_mod.check_tokenizer_fingerprint()


class TestFingerprintBaselineFile:
    """The pinned baseline must ship with the package and be parseable."""

    def test_baseline_file_exists_and_parses(self):
        from pathlib import Path
        import yaml

        from api_relay_audit import pricing as pricing_pkg
        baseline = Path(pricing_pkg.__file__).parent / "tokenizer_fingerprint.yaml"
        assert baseline.exists(), (
            "tokenizer_fingerprint.yaml is missing — R5 落地点 2 requires "
            "the pinned baseline to ship with the package."
        )
        data = yaml.safe_load(baseline.read_text(encoding="utf-8"))
        assert isinstance(data, dict)
        # At minimum: tiktoken + anthropic pinned.
        assert "tiktoken" in data
        assert "anthropic" in data
