"""Story-1 (S1-probe-core/types) schema + dataclass freeze regression tests.

Acceptance mapping (PRD §6.2 + Story-1 description):

- A1 module entry: ``from api_relay_audit.probe import run`` exposes a
  non-empty docstring describing the contract.
- A3 schema check: ``api_relay_audit.probe.schema_check`` validates the
  PRD §3.5 JSON draft.
- schema_version == ``"1.0"`` is asserted character-by-character.
- dual-distribution: standalone ``audit.py`` mirrors the dataclass
  definitions inside the ``# === probe types ===`` Section block; the
  parity check lives in ``tests/test_dual_distribution_parity.py``.

These tests do NOT import any of Story-2..Story-6 probe implementations.
They only exercise the frozen contract surface that downstream slices
will consume.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# A1 module entry: probe.run has a non-empty docstring
# ---------------------------------------------------------------------------


def test_probe_module_exports_run_with_docstring():
    """PRD §6.2 A1: `python -c "from api_relay_audit.probe import run; print(run.__doc__)"`
    outputs a non-empty docstring. Story-1 ships a stub; Story-6 fills in
    the body. The docstring contract is frozen here.
    """
    from api_relay_audit.probe import run

    assert callable(run), "probe.run must be callable"
    assert run.__doc__ is not None, "probe.run must have a docstring"
    assert run.__doc__.strip(), "probe.run docstring must be non-empty"
    # Story-1 contract: docstring must mention the return type ProbeReport
    # so callers know what shape comes back. Frozen by this test.
    assert "ProbeReport" in run.__doc__, (
        "probe.run docstring must reference its return type 'ProbeReport'"
    )


# ---------------------------------------------------------------------------
# schema_version == "1.0" frozen contract
# ---------------------------------------------------------------------------


def test_schema_version_is_frozen_to_one_dot_zero():
    """Character-level assertion: ProbeReport.SCHEMA_VERSION == "1.0".
    Bumping this requires a v5 design cycle (design §4.2)."""
    from api_relay_audit.probe.types import ProbeReport

    assert ProbeReport.SCHEMA_VERSION == "1.0"
    # Equivalent character-level check on the source-of-truth schema file.
    schema_path = (
        REPO_ROOT
        / "api_relay_audit"
        / "probe"
        / "references"
        / "schema.json"
    )
    with schema_path.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    pinned = schema["properties"]["schema_version"]["const"]
    assert pinned == "1.0", (
        f"schema.json schema_version const = {pinned!r}; design freezes it to '1.0'"
    )


# ---------------------------------------------------------------------------
# Top-level ProbeReport keys mirror PRD §3.5
# ---------------------------------------------------------------------------


REQUIRED_TOP_LEVEL_KEYS = (
    "schema_version",
    "generated_at",
    "input_base_url",
    "input_key_fingerprint",
    "input_vendor_hint",
    "verdict",
    "reachability",
    "auth_sniff",
    "models_diff",
    "rate_limit",
    # infra_hint is optional (PRD §3.5 派生字段); total_http_calls is bookkeeping.
    "total_http_calls",
)


def _minimal_report():
    """Build a minimal valid ProbeReport with default dataclass values."""
    from api_relay_audit.probe.types import (
        AuthSniffResult,
        ModelsDiffResult,
        ProbeReport,
        RateLimitResult,
        ReachabilityResult,
    )

    return ProbeReport(
        schema_version="1.0",
        generated_at="2026-05-24T10:00:00Z",
        input_base_url="https://relay.example.com/v1",
        input_key_fingerprint="sha256:8f00aa11",
        input_vendor_hint="auto",
        verdict="pass",
        reachability=ReachabilityResult(status="ok", tcp_ok=True, tls_ok=True),
        auth_sniff=AuthSniffResult(status="ok"),
        models_diff=ModelsDiffResult(status="ok"),
        rate_limit=RateLimitResult(status="ok"),
    )


def test_probe_report_to_dict_has_required_top_level_keys():
    report = _minimal_report()
    payload = report.to_dict()
    for key in REQUIRED_TOP_LEVEL_KEYS:
        assert key in payload, f"ProbeReport.to_dict() missing required key {key!r}"
    # schema_version must be a string, not the SCHEMA_VERSION classvar reference.
    assert payload["schema_version"] == "1.0"
    assert payload["verdict"] == "pass"
    # 4 ProbeResult sub-objects must serialize as dicts with their own status.
    for k in ("reachability", "auth_sniff", "models_diff", "rate_limit"):
        assert isinstance(payload[k], dict)
        assert payload[k]["status"] == "ok"


def test_probe_report_to_dict_serializes_infra_hint_only_when_present():
    """PRD §3.5: infra_hint is a derived field that MAY be absent.
    When absent it must not appear in the JSON (avoid `null` confusion)."""
    report = _minimal_report()
    assert "infra_hint" not in report.to_dict()

    from api_relay_audit.probe.types import InfraHint

    report.infra_hint = InfraHint(framework="new-api", confidence="confirmed")
    payload = report.to_dict()
    assert payload["infra_hint"] == {
        "framework": "new-api",
        "confidence": "confirmed",
    }


def test_probe_report_has_fatal_reflects_verdict():
    report = _minimal_report()
    assert report.has_fatal() is False
    report.verdict = "fail"
    assert report.has_fatal() is True
    report.verdict = "warn"
    assert report.has_fatal() is False


# ---------------------------------------------------------------------------
# ProbeResult dataclasses share the `status` field (PRD §4.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls_name",
    ["ReachabilityResult", "AuthSniffResult", "ModelsDiffResult", "RateLimitResult"],
)
def test_probe_result_dataclasses_have_status_field(cls_name):
    """PRD §4.2: every ProbeResult must carry status ∈ {ok, degraded, error}.
    This test enforces the *structure*; value enumeration is asserted below.
    """
    import api_relay_audit.probe.types as types_mod

    cls = getattr(types_mod, cls_name)
    assert is_dataclass(cls), f"{cls_name} must be a @dataclass"
    field_names = {f.name for f in fields(cls)}
    assert "status" in field_names, f"{cls_name} missing required field 'status'"
    assert "error" in field_names, (
        f"{cls_name} must carry an optional 'error' field (PRD §4.2 禁止吞错误)"
    )


def test_probe_status_constants():
    from api_relay_audit.probe.types import PROBE_STATUS

    # PRD §4.2 hard constraint: tri-state, identical order across distributions.
    assert PROBE_STATUS == ("ok", "degraded", "error")


def test_probe_verdict_constants():
    from api_relay_audit.probe.types import PROBE_VERDICT

    # PRD §3.3 / §6.2 hard constraint.
    assert PROBE_VERDICT == ("pass", "warn", "fail")


# ---------------------------------------------------------------------------
# JSON Schema file is well-formed and describes the same shape
# ---------------------------------------------------------------------------


def test_schema_json_is_valid_and_lists_required_keys():
    schema_path = (
        REPO_ROOT
        / "api_relay_audit"
        / "probe"
        / "references"
        / "schema.json"
    )
    with schema_path.open("r", encoding="utf-8") as f:
        schema = json.load(f)
    assert schema.get("$schema"), "schema.json must declare a $schema dialect"
    assert schema.get("type") == "object"
    required = set(schema.get("required", []))
    # Every required key in the schema must also be a top-level key
    # in ProbeReport.to_dict() (excluding the optional infra_hint).
    for key in (
        "schema_version",
        "generated_at",
        "input_base_url",
        "input_key_fingerprint",
        "input_vendor_hint",
        "verdict",
        "reachability",
        "auth_sniff",
        "models_diff",
        "rate_limit",
    ):
        assert key in required, f"schema.json must require {key!r}"


# ---------------------------------------------------------------------------
# schema_check.py validates the PRD §3.5 JSON draft (A3)
# ---------------------------------------------------------------------------


PRD_DRAFT_JSON = {
    "schema_version": "1.0",
    "generated_at": "2026-05-24T10:00:00Z",
    "input_base_url": "https://relay.example.com/v1",
    "input_key_fingerprint": "sha256:8f00aa11",
    "input_vendor_hint": "auto",
    "verdict": "pass",
    "reachability": {
        "status": "ok",
        "tcp_ok": True,
        "tls_ok": True,
        "latency_ms": 120,
        "dns_resolves": True,
        "fallback_to_curl": False,
        "signals": [],
    },
    "auth_sniff": {
        "status": "ok",
        "accepted_schemes": ["bearer"],
        "envelope_401": "openai-style",
        "envelope_403": "unknown",
        "key_position": "Authorization",
        "classification": "strict",
        "signals": [],
    },
    "models_diff": {
        "status": "ok",
        "declared": ["gpt-4o"],
        "declared_count": 1,
        "official_reference": ["gpt-4o"],
        "extra_in_relay": [],
        "missing_in_relay": [],
        "suspicious_aliases": [],
        "claimed_model_match": "exact",
        "vendor_breakdown": {"gpt": 1},
        "signals": [],
    },
    "rate_limit": {
        "status": "ok",
        "rpm_observed": 60,
        "headers_seen": ["x-ratelimit-limit-requests"],
        "envelope_429": "openai-style",
        "retry_after_pattern": "fixed-seconds",
        "burst_window_s": None,
        "triggered_429": False,
        "samples_to_429": None,
        "compliance": "openai-compatible",
        "probe_disabled": False,
        "signals": [],
    },
    "infra_hint": {"framework": "new-api", "confidence": "confirmed"},
    "total_http_calls": 17,
}


def test_schema_check_accepts_prd_draft_json():
    """PRD §6.2 A3: schema_check passes on the §3.5 reference JSON shape."""
    from api_relay_audit.probe.schema_check import validate

    validate(PRD_DRAFT_JSON)  # must not raise


def test_schema_check_accepts_minimal_report_round_trip():
    from api_relay_audit.probe.schema_check import validate

    payload = _minimal_report().to_dict()
    validate(payload)  # round-trip must not raise


def test_schema_check_rejects_missing_schema_version():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = dict(PRD_DRAFT_JSON)
    del payload["schema_version"]
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "schema_version" in str(exc_info.value)


def test_schema_check_rejects_wrong_schema_version():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = dict(PRD_DRAFT_JSON)
    payload["schema_version"] = "0.9"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    msg = str(exc_info.value)
    assert "schema_version" in msg
    assert "1.0" in msg


def test_schema_check_rejects_unknown_verdict():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = dict(PRD_DRAFT_JSON)
    payload["verdict"] = "yolo"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "verdict" in str(exc_info.value)


def test_schema_check_rejects_missing_required_top_level_key():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = dict(PRD_DRAFT_JSON)
    del payload["reachability"]
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "reachability" in str(exc_info.value)


def test_schema_check_rejects_sub_result_missing_status():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))  # deep copy
    del payload["reachability"]["status"]
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    msg = str(exc_info.value)
    assert "status" in msg
    assert "reachability" in msg


def test_schema_check_rejects_invalid_sub_result_status_value():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["auth_sniff"]["status"] = "broken"  # not in PROBE_STATUS tuple
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "auth_sniff" in str(exc_info.value)
    assert "status" in str(exc_info.value)


def test_schema_check_rejects_non_object():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    with pytest.raises(SchemaValidationError):
        validate([])
    with pytest.raises(SchemaValidationError):
        validate("not a dict")
    with pytest.raises(SchemaValidationError):
        validate(None)


# ---------------------------------------------------------------------------
# schema_check CLI entry point: `python -m api_relay_audit.probe.schema_check`
# ---------------------------------------------------------------------------


def test_schema_check_cli_accepts_valid_payload(tmp_path):
    fixture = tmp_path / "probe.json"
    fixture.write_text(json.dumps(PRD_DRAFT_JSON), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "api_relay_audit.probe.schema_check", str(fixture)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, (
        f"CLI exited {result.returncode}; stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def test_schema_check_cli_rejects_invalid_payload(tmp_path):
    fixture = tmp_path / "bad.json"
    bad = dict(PRD_DRAFT_JSON)
    bad["schema_version"] = "0.9"
    fixture.write_text(json.dumps(bad), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "api_relay_audit.probe.schema_check", str(fixture)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode != 0, "CLI must signal failure on schema mismatch"
    combined = result.stdout + result.stderr
    assert "schema_version" in combined


# ---------------------------------------------------------------------------
# Optional fields must not silently leak raw key material (PRD §4.2 / §5.1 C2)
# ---------------------------------------------------------------------------


def test_probe_report_key_fingerprint_only_carries_sha256_prefix():
    """Constructing a ProbeReport never accepts a raw key. The contract is
    `input_key_fingerprint` only; raw keys are filtered at the entry
    `probe.run()` boundary in Story-6. Here we assert the dataclass has
    no field named `key`, `api_key`, or similar to make the contract
    surface-visible to reviewers.
    """
    from api_relay_audit.probe.types import ProbeReport

    field_names = {f.name for f in fields(ProbeReport)}
    forbidden = {"key", "api_key", "raw_key", "secret", "token"}
    leaked = field_names & forbidden
    assert not leaked, (
        f"ProbeReport must not declare raw-key fields; leaked: {leaked!r}"
    )


# ---------------------------------------------------------------------------
# probe.run() stub: signature is frozen, body is Story-6 territory
# ---------------------------------------------------------------------------


def test_probe_run_stub_raises_not_implemented():
    """Story-1 ships the call signature only; calling it must raise
    NotImplementedError so Story-6 work cannot be silently merged with a
    no-op body."""
    from api_relay_audit.probe import run

    with pytest.raises(NotImplementedError):
        run("https://example.com/v1", "sk-test")


# ---------------------------------------------------------------------------
# schema_check internals: _main() + edge-case branches
# ---------------------------------------------------------------------------


def test_schema_check_main_succeeds_on_valid_payload(tmp_path):
    from api_relay_audit.probe import schema_check

    fixture = tmp_path / "ok.json"
    fixture.write_text(json.dumps(PRD_DRAFT_JSON), encoding="utf-8")
    assert schema_check._main([str(fixture)]) == 0


def test_schema_check_main_fails_on_invalid_payload(tmp_path):
    from api_relay_audit.probe import schema_check

    fixture = tmp_path / "bad.json"
    bad = dict(PRD_DRAFT_JSON)
    bad["verdict"] = "yolo"
    fixture.write_text(json.dumps(bad), encoding="utf-8")
    assert schema_check._main([str(fixture)]) == 2


def test_schema_check_main_usage_on_wrong_argc():
    from api_relay_audit.probe import schema_check

    assert schema_check._main([]) == 64
    assert schema_check._main(["a", "b"]) == 64


def test_schema_check_main_handles_missing_file(tmp_path):
    from api_relay_audit.probe import schema_check

    missing = tmp_path / "does_not_exist.json"
    assert schema_check._main([str(missing)]) == 1


def test_schema_check_main_handles_invalid_json(tmp_path):
    from api_relay_audit.probe import schema_check

    fixture = tmp_path / "broken.json"
    fixture.write_text("not json {{{", encoding="utf-8")
    assert schema_check._main([str(fixture)]) == 1


def test_schema_check_rejects_bool_for_integer_field():
    """JSON Schema 'integer' must not accept Python booleans even though
    bool is a subclass of int."""
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["total_http_calls"] = True
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "integer" in str(exc_info.value).lower()


def test_schema_check_rejects_negative_total_http_calls():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["total_http_calls"] = -1
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "minimum" in str(exc_info.value)


def test_schema_check_rejects_invalid_vendor_hint():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["input_vendor_hint"] = "lobechat"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "vendor_hint" in str(exc_info.value)


def test_schema_check_rejects_malformed_infra_hint_confidence():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["infra_hint"]["confidence"] = "sort-of-confirmed"
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "confidence" in str(exc_info.value)


def test_schema_check_accepts_explicit_null_error_in_sub_result():
    """PRD §4.2 ProbeResult.error is optional; the JSON shape allows
    explicit null in addition to omission."""
    from api_relay_audit.probe.schema_check import validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["reachability"]["error"] = None
    validate(payload)


def test_schema_check_accepts_populated_error_object():
    from api_relay_audit.probe.schema_check import validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["reachability"]["status"] = "error"
    payload["reachability"]["error"] = {
        "code": "tcp_refused",
        "message": "connection refused",
    }
    validate(payload)


def test_schema_check_rejects_error_object_missing_code():
    from api_relay_audit.probe.schema_check import SchemaValidationError, validate

    payload = json.loads(json.dumps(PRD_DRAFT_JSON))
    payload["reachability"]["error"] = {"message": "boom"}
    with pytest.raises(SchemaValidationError) as exc_info:
        validate(payload)
    assert "code" in str(exc_info.value)


def test_schema_check_bad_ref_branch_is_defended():
    """Internal: feeding the validator a schema with a malformed $ref
    must raise rather than silently passing."""
    from api_relay_audit.probe.schema_check import (
        SchemaValidationError,
        _check,
    )

    with pytest.raises(SchemaValidationError):
        _check({}, {"$ref": "external://nope"}, "", {})
    with pytest.raises(SchemaValidationError):
        _check({}, {"$ref": "#/$defs/Missing"}, "", {"$defs": {}})


def test_schema_check_unsupported_type_branch_is_defended():
    from api_relay_audit.probe.schema_check import SchemaValidationError, _check

    with pytest.raises(SchemaValidationError):
        _check("x", {"type": "weird"}, "field", {})

