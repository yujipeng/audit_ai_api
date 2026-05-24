"""Tests for security.secrets — YAML credential loading with fail-fast.

AC-S5-002:
  * Plaintext API keys inside YAML are rejected at load time with the line number.
  * `$ENV{X}` references resolve from os.environ.
  * Undefined `$ENV{X}` references fail-fast with the line number of the reference.
"""

from __future__ import annotations

import os

import pytest

from security.secrets import (
    PlaintextCredentialError,
    UndefinedEnvReferenceError,
    load_secrets_yaml,
    resolve_env_references,
)


class TestEnvReferenceResolution:
    def test_env_reference_resolved(self, monkeypatch):
        monkeypatch.setenv("OPENAI_KEY", "sk-RESOLVED-VALUE-abcdefghij1234567890")
        assert (
            resolve_env_references("$ENV{OPENAI_KEY}")
            == "sk-RESOLVED-VALUE-abcdefghij1234567890"
        )

    def test_env_reference_undefined_raises_with_name(self, monkeypatch):
        monkeypatch.delenv("MISSING_KEY", raising=False)
        with pytest.raises(UndefinedEnvReferenceError) as exc:
            resolve_env_references("$ENV{MISSING_KEY}")
        assert "MISSING_KEY" in str(exc.value)

    def test_passthrough_when_no_reference(self):
        assert resolve_env_references("plain text") == "plain text"

    def test_partial_reference_inside_string(self, monkeypatch):
        monkeypatch.setenv("FOO", "BAR")
        assert resolve_env_references("prefix-$ENV{FOO}-suffix") == "prefix-BAR-suffix"


class TestLoadSecretsYAML:
    def test_plaintext_openai_key_fail_fast(self, tmp_path):
        p = tmp_path / "secrets.yaml"
        p.write_text(
            "name: relay\n"
            "endpoints:\n"
            "  primary:\n"
            "    api_key: sk-PLAINTEXT0123456789ABCDEFGHIJ\n"  # line 4
        )
        with pytest.raises(PlaintextCredentialError) as exc:
            load_secrets_yaml(str(p))
        msg = str(exc.value)
        assert "line 4" in msg or "line: 4" in msg
        assert "api_key" in msg

    def test_plaintext_bearer_token_fail_fast(self, tmp_path):
        p = tmp_path / "secrets.yaml"
        p.write_text(
            "auth:\n"
            "  header: 'Bearer eyJabcdefghij1234567890KLMNOP.payload.sig'\n"  # line 2
        )
        with pytest.raises(PlaintextCredentialError) as exc:
            load_secrets_yaml(str(p))
        assert "line 2" in str(exc.value)

    def test_env_reference_undefined_with_line_number(self, tmp_path, monkeypatch):
        monkeypatch.delenv("RELAY_KEY", raising=False)
        p = tmp_path / "secrets.yaml"
        p.write_text(
            "name: relay\n"
            "endpoints:\n"
            "  - name: a\n"
            "    api_key: $ENV{RELAY_KEY}\n"  # line 4
        )
        with pytest.raises(UndefinedEnvReferenceError) as exc:
            load_secrets_yaml(str(p))
        msg = str(exc.value)
        assert "RELAY_KEY" in msg
        assert "line 4" in msg or "line: 4" in msg

    def test_env_reference_resolves_clean(self, tmp_path, monkeypatch):
        monkeypatch.setenv("RELAY_KEY", "sk-RESOLVED-abcdefghij1234567890ZZZZ")
        p = tmp_path / "secrets.yaml"
        p.write_text(
            "name: relay\n"
            "endpoints:\n"
            "  - name: a\n"
            "    api_key: $ENV{RELAY_KEY}\n"
        )
        cfg = load_secrets_yaml(str(p))
        assert cfg["endpoints"][0]["api_key"] == "sk-RESOLVED-abcdefghij1234567890ZZZZ"

    def test_loaded_object_does_not_expose_plaintext_in_repr(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("RELAY_KEY", "sk-PRIVATE-abcdefghij1234567890ZZZZ")
        p = tmp_path / "secrets.yaml"
        p.write_text(
            "endpoints:\n  - name: a\n    api_key: $ENV{RELAY_KEY}\n"
        )
        cfg = load_secrets_yaml(str(p), wrap_credentials=True)
        bearer = cfg["endpoints"][0]["api_key"]
        # When wrapped, repr/str must not contain the secret.
        assert "sk-PRIVATE-abcdefghij1234567890ZZZZ" not in repr(bearer)
        assert "sk-PRIVATE-abcdefghij1234567890ZZZZ" not in str(bearer)
        assert bearer.reveal() == "sk-PRIVATE-abcdefghij1234567890ZZZZ"
