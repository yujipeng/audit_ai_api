"""Credential isolation tests — design §2.5.2 rules 1–4 + sentinel fuzz.

The four injection-isolation rules:
  1. CredentialBearer wraps secrets so repr / str / serialization / logging never expose them.
  2. Subprocess invocations pass credentials via env only — never argv.
  3. Functions that accept credentials use explicit kwargs (no **kwargs swallow).
  4. Sentinel fuzz: when a known sentinel key is the configured run.key, no audit
     artifact (report.html/json, diff.md, transparent.jsonl, baseline/*.json,
     stderr) may contain the sentinel.

The sentinel fuzz here exercises the redact + bearer + safe-subprocess-env
chain end-to-end without depending on the (not-yet-wired) S5-B/C/D code paths.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys

import pytest

from security.bearer import (
    CredentialBearer,
    require_explicit_credential_kwargs,
    safe_subprocess_env,
)
from security.redact import (
    redact_response_text,
    redact_text,
    redact_url,
)


SENTINEL = "SENTINEL-XYZ-DO-NOT-LEAK"


class TestRule1_CredentialBearer:
    def test_repr_does_not_leak(self):
        b = CredentialBearer("sk-supersecretABCDEFGHIJ1234567890")
        assert "sk-supersecretABCDEFGHIJ1234567890" not in repr(b)
        assert "sk-supersecretABCDEFGHIJ1234567890" not in str(b)

    def test_logging_does_not_leak(self, caplog):
        b = CredentialBearer(SENTINEL)
        with caplog.at_level(logging.DEBUG):
            logging.getLogger("test").debug("got cred %s and also %r", b, b)
        for rec in caplog.records:
            assert SENTINEL not in rec.getMessage()

    def test_format_does_not_leak(self):
        b = CredentialBearer(SENTINEL)
        assert SENTINEL not in f"{b}"
        assert SENTINEL not in "{}".format(b)
        assert SENTINEL not in f"{b!r}"

    def test_serialization_blocked(self):
        # Serializing a credential is a foot-gun; __reduce_ex__ must refuse.
        b = CredentialBearer(SENTINEL)
        with pytest.raises(TypeError):
            b.__reduce_ex__(2)
        with pytest.raises(TypeError):
            b.__reduce__()

    def test_reveal_returns_plaintext(self):
        b = CredentialBearer(SENTINEL)
        assert b.reveal() == SENTINEL

    def test_redacted_id_is_stable_and_short(self):
        b1 = CredentialBearer(SENTINEL)
        b2 = CredentialBearer(SENTINEL)
        assert b1.redacted_id == b2.redacted_id
        assert len(b1.redacted_id) == 8
        # Different values produce different ids
        b3 = CredentialBearer("different-value-here-1234567890")
        assert b1.redacted_id != b3.redacted_id

    def test_redacted_id_does_not_leak_plaintext(self):
        b = CredentialBearer(SENTINEL)
        rid = b.redacted_id
        assert SENTINEL[:8] != rid
        assert SENTINEL not in rid


class TestRule2_EnvOnlySubprocess:
    def test_safe_env_inherits_only_allowlist(self):
        env = safe_subprocess_env({"OPENAI_API_KEY": CredentialBearer(SENTINEL)})
        assert "PATH" in env
        assert env["OPENAI_API_KEY"] == SENTINEL

    def test_safe_env_strips_unrelated_secrets(self, monkeypatch):
        monkeypatch.setenv("UNRELATED_TOKEN", "sk-unrelated-abcdefghij1234567890ZZZZ")
        env = safe_subprocess_env({"OPENAI_API_KEY": CredentialBearer(SENTINEL)})
        assert "UNRELATED_TOKEN" not in env

    def test_safe_env_accepts_plain_string(self):
        env = safe_subprocess_env({"K": "plain-value"})
        assert env["K"] == "plain-value"

    def test_subprocess_argv_does_not_carry_credential(self):
        """Smoke test the design rule: credential reaches the child only via env."""
        env = safe_subprocess_env({"MY_KEY": CredentialBearer(SENTINEL)})
        out = subprocess.run(
            [sys.executable, "-c", "import os; print(len(os.environ['MY_KEY']))"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert out.stdout.strip() == str(len(SENTINEL))


class TestRule3_ExplicitKwargs:
    def test_decorator_rejects_var_keyword(self):
        with pytest.raises(TypeError):

            @require_explicit_credential_kwargs(("api_key",))
            def f(**kwargs):
                return kwargs

    def test_decorator_accepts_explicit_kwarg(self):
        @require_explicit_credential_kwargs(("api_key",))
        def f(*, api_key, model="default"):
            return api_key, model

        assert f(api_key="x", model="m") == ("x", "m")

    def test_decorator_rejects_missing_named_cred(self):
        with pytest.raises(TypeError):

            @require_explicit_credential_kwargs(("api_key",))
            def f(*, model="default"):
                return model


class TestRule4_SentinelFuzz:
    """End-to-end: take a sentinel key, feed it through the artifact-rendering primitives,
    and grep the output for any survival of the sentinel."""

    def _render_artifacts(self, sentinel: str) -> dict[str, str]:
        bearer = CredentialBearer(sentinel)
        response_body = (
            f"Echoing back: your key was {sentinel}. "
            "I am Claude, an AI assistant."
        )
        request_url = (
            f"https://relay.example.com/v1/chat?api_key={sentinel}&model=opus"
        )
        request_headers = (
            f"Authorization: Bearer {sentinel}\n"
            f"x-api-key: {sentinel}\n"
        )
        logged_diag = f"endpoint config: api_key={sentinel}"
        # Any rendering surface that may carry model-controlled text MUST go
        # through redact_response_text (which knows the run's sentinel keys);
        # surfaces carrying only our own diagnostics use redact_text.
        return {
            "report.html": (
                redact_response_text(response_body, {sentinel})
                + redact_text(logged_diag)
            ),
            "report.json": json.dumps(
                {
                    "redacted_key_id": bearer.redacted_id,
                    "echoed": redact_response_text(response_body, {sentinel}),
                    "url": redact_url(request_url),
                    "bearer_repr": repr(bearer),
                }
            ),
            "diff.md": redact_response_text(
                f"--- baseline\n+++ current\n {response_body}", {sentinel}
            ),
            "transparent.jsonl": json.dumps(
                {
                    "hash_only": bearer.redacted_id,
                    "headers": redact_response_text(request_headers, {sentinel}),
                }
            ),
            "baseline/run.json": json.dumps(
                {
                    "redacted_key_id": bearer.redacted_id,
                    "url": redact_url(request_url),
                }
            ),
            "stderr": redact_response_text(
                f"http error 401, sent key {sentinel}", {sentinel}
            ),
        }

    def test_no_sentinel_in_any_artifact(self):
        artifacts = self._render_artifacts(SENTINEL)
        for name, body in artifacts.items():
            assert SENTINEL not in body, f"sentinel leaked in {name}: {body!r}"

    def test_artifacts_still_carry_redacted_id(self):
        artifacts = self._render_artifacts(SENTINEL)
        bearer_id = CredentialBearer(SENTINEL).redacted_id
        assert bearer_id in artifacts["report.json"]
        assert bearer_id in artifacts["transparent.jsonl"]
        assert bearer_id in artifacts["baseline/run.json"]

    def test_fuzz_variant_sentinels(self):
        """Vary the sentinel to catch hard-coded redactors."""
        for variant in [
            "SENTINEL-A-DO-NOT-LEAK-12345678",
            "fuzz-key-abcdefghij1234567890ZZZZ",
            "sk-fuzzABCDEFGHIJ1234567890ZZZZ",
        ]:
            artifacts = self._render_artifacts(variant)
            for name, body in artifacts.items():
                assert variant not in body, f"variant {variant} leaked in {name}"


class TestRule4b_SentinelFuzz_S5C_RenderPipelines:
    """Same fuzz contract, but routed through the real S5-C render pipelines.

    Where `TestRule4_SentinelFuzz` exercises the redact + bearer primitives in
    isolation, this class wires the actual `reporting.render_markdown` /
    `reporting.render_html` / `orchestration.baseline` entry points so a
    regression in any one downstream surface (e.g. a new payload field that
    bypasses the existing redact call site) trips here.
    """

    @staticmethod
    def _polluted_record(sentinel: str) -> dict:
        from orchestration.run_record import new_cell, new_record

        rec = new_record(
            code_version="fuzz",
            config_digest="cfg-fuzz",
            redacted_key_ids=["aaaa1111"],
        )
        c = new_cell(
            step="probe",
            endpoint="alpha",
            model="claude-opus-4-6",
            redacted_key_id="aaaa1111",
            schema_version=1,
            code_version="fuzz",
        )
        c["status"] = "ok"
        c["latency_ms"] = 100.0
        c["payload"] = {
            "score": 0.9,
            "note": f"echoed key={sentinel}",
            "raw_response": (
                f"Authorization: Bearer {sentinel}; "
                f"GET https://relay/api?api_key={sentinel}"
            ),
        }
        rec["cells"].append(c)

        err_cell = new_cell(
            step="purity",
            endpoint="alpha",
            model="claude-opus-4-6",
            redacted_key_id="aaaa1111",
            schema_version=1,
            code_version="fuzz",
        )
        err_cell["status"] = "error"
        err_cell["latency_ms"] = 50.0
        err_cell["error"] = {
            "type": "RuntimeError",
            "message": f"http 401, sent key {sentinel}",
            "traceback": f"Traceback: leaked {sentinel}",
        }
        rec["cells"].append(err_cell)
        return rec

    def test_sentinel_does_not_leak_in_markdown(self):
        from reporting.render_markdown import render

        rec = self._polluted_record(SENTINEL)
        out = render(rec, sentinel_keys=[SENTINEL])
        assert SENTINEL not in out, "sentinel leaked in MD render"

    def test_sentinel_does_not_leak_in_html(self):
        from reporting.render_html import render

        rec = self._polluted_record(SENTINEL)
        out = render(rec, sentinel_keys=[SENTINEL])
        assert SENTINEL not in out, "sentinel leaked in HTML render"

    def test_sentinel_does_not_leak_in_diff_md(self):
        from orchestration.baseline import compute_diff, render_diff_md

        baseline = self._polluted_record(SENTINEL)
        current = self._polluted_record(SENTINEL)
        # Push the current run off baseline so the diff body has live cells.
        current["cells"][0]["latency_ms"] = 1000.0
        current["cells"][0]["payload"]["score"] = 0.5
        diff = compute_diff(
            current=current, baseline=baseline, sentinel_keys=[SENTINEL]
        )
        out = render_diff_md(diff)
        assert SENTINEL not in out, "sentinel leaked in diff.md render"

    @pytest.mark.parametrize(
        "variant",
        [
            "SENTINEL-A-DO-NOT-LEAK-12345678",
            "fuzz-key-abcdefghij1234567890ZZZZ",
            "sk-fuzzABCDEFGHIJ1234567890ZZZZ",
        ],
    )
    def test_variant_sentinels(self, variant):
        """Vary the sentinel to catch hard-coded redactors in the S5-C surfaces."""
        from orchestration.baseline import compute_diff, render_diff_md
        from reporting.render_html import render as render_html
        from reporting.render_markdown import render as render_md

        rec = self._polluted_record(variant)
        assert variant not in render_md(rec, sentinel_keys=[variant])
        assert variant not in render_html(rec, sentinel_keys=[variant])

        cur = self._polluted_record(variant)
        cur["cells"][0]["latency_ms"] = 1000.0
        diff = compute_diff(current=cur, baseline=rec, sentinel_keys=[variant])
        assert variant not in render_diff_md(diff)
