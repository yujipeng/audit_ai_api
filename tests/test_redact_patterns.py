"""Pattern-level tests for security/redact.py — the 4 credential regex families.

Coverage matrix (issue TES-127 AC-S5-008):
  1. openai-key       sk-XXXXXXXXXX...                 → REDACTED
  2. anthropic-key    sk-ant-XXXXXXXXXX...             → REDACTED
  3. Bearer token     Authorization: Bearer XXX...     → REDACTED
  4. custom-header    x-api-key: XXXX                  → REDACTED
  5. query-param key  ?api_key=XXXX in URLs            → REDACTED
  6. model-echoed key sentinel substring in response   → REDACTED (sentinel_keys path)
"""

from __future__ import annotations

import re

import pytest

from security.redact import (
    REDACT_PLACEHOLDER,
    redact_response_text,
    redact_text,
    redact_url,
)


class TestOpenAIKeyPattern:
    def test_openai_key_redacted_inline(self):
        s = "calling api with sk-1234567890ABCDEFghij and continuing"
        out = redact_text(s)
        assert "sk-1234567890ABCDEFghij" not in out
        assert REDACT_PLACEHOLDER in out

    def test_openai_proj_key_redacted(self):
        s = "key=sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
        out = redact_text(s)
        assert "sk-proj-AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" not in out

    def test_short_sk_prefix_not_overmatched(self):
        # 19 chars after prefix → below 20-char threshold, must NOT match
        s = "see sk-shortabcdefghij comment"
        out = redact_text(s)
        assert "sk-shortabcdefghij" in out


class TestAnthropicKeyPattern:
    def test_anthropic_key_redacted(self):
        s = "auth=sk-ant-api03-abcdefghijKLMNOPqrstuvwxyz0123456789-AAAA"
        out = redact_text(s)
        assert "sk-ant-api03-abcdefghijKLMNOPqrstuvwxyz0123456789-AAAA" not in out
        assert REDACT_PLACEHOLDER in out


class TestBearerTokenPattern:
    def test_bearer_token_redacted(self):
        s = "Authorization: Bearer eyJabcdefghij1234567890KLMNOPQRSTUV.payload.sig"
        out = redact_text(s)
        assert "eyJabcdefghij1234567890KLMNOPQRSTUV.payload.sig" not in out
        # The literal token "Bearer" stays as a label, only the secret is replaced
        assert "Bearer" in out
        assert REDACT_PLACEHOLDER in out

    def test_bearer_short_token_not_matched(self):
        # 10 chars → below threshold
        s = "Authorization: Bearer abc1234567"
        out = redact_text(s)
        assert "abc1234567" in out


class TestCustomHeaderKeyPattern:
    def test_x_api_key_header_redacted(self):
        s = "x-api-key: aabbccddeeff00112233445566778899abcd"
        out = redact_text(s)
        assert "aabbccddeeff00112233445566778899abcd" not in out
        assert REDACT_PLACEHOLDER in out

    def test_api_key_underscore_redacted(self):
        s = "api_key='ZZyyXXwwVVuuTTssRRqqPPooNNmmLLkk'"
        out = redact_text(s)
        assert "ZZyyXXwwVVuuTTssRRqqPPooNNmmLLkk" not in out


class TestRedactURL:
    def test_url_query_key_redacted(self):
        u = "https://relay.example.com/v1/chat?api_key=abcdef0123456789ZZZZZZZZZZZZZZZZ&model=gpt-4"
        out = redact_url(u)
        assert "abcdef0123456789ZZZZZZZZZZZZZZZZ" not in out
        assert "model=gpt-4" in out
        assert "relay.example.com" in out

    def test_url_userinfo_redacted(self):
        u = "https://user:sk-1234567890ABCDEFGHIJklmn@relay.example.com/v1"
        out = redact_url(u)
        # The whole userinfo is stripped (auth in URL is unsafe even if it doesn't match a known pattern)
        assert "sk-1234567890ABCDEFGHIJklmn" not in out

    def test_url_no_secret_passthrough(self):
        u = "https://relay.example.com/v1/models"
        out = redact_url(u)
        assert out == u


class TestSentinelEcho:
    """Model echoing back the API key as plaintext — the most damaging exfiltration."""

    def test_sentinel_in_response_text_redacted(self):
        sentinel = "SENTINEL-XYZ-DO-NOT-LEAK"
        body = f"I am Claude. The key you sent me was {sentinel} and I should not have echoed it."
        out = redact_response_text(body, sentinel_keys={sentinel})
        assert sentinel not in out
        assert REDACT_PLACEHOLDER in out

    def test_sentinel_partial_substring_redacted(self):
        # Even a partial substring (≥ 16 chars contiguous) leaks the key — must be redacted.
        sentinel = "SK-CONFIDENTIAL-VALUE-1234567890"
        body = f"prefix {sentinel[:20]} suffix"  # 20-char chunk
        out = redact_response_text(body, sentinel_keys={sentinel})
        assert sentinel[:20] not in out

    def test_sentinel_plus_pattern_both_apply(self):
        sentinel = "MY-RUN-KEY-ABCDEFGHIJKLMNOP"
        body = f"{sentinel} also sk-1234567890ABCDEFGHIJKLMN"
        out = redact_response_text(body, sentinel_keys={sentinel})
        assert sentinel not in out
        assert "sk-1234567890ABCDEFGHIJKLMN" not in out

    def test_empty_sentinel_set_falls_back_to_patterns(self):
        body = "sk-abcdefghij1234567890ZZZZZZZZ leaked"
        out = redact_response_text(body, sentinel_keys=set())
        assert "sk-abcdefghij1234567890ZZZZZZZZ" not in out


class TestRedactReversible:
    """Redaction MUST be one-way; the original credential must not be reconstructable."""

    def test_no_partial_key_leaks(self):
        secret = "sk-FULLSECRETabcdef1234567890ZZZZZ"
        out = redact_text(f"value={secret}")
        # No 8+ char substring of the secret should survive
        for i in range(len(secret) - 7):
            assert secret[i : i + 8] not in out, f"leaked window: {secret[i:i+8]}"
