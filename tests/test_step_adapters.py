"""Tests for orchestration.steps — StepResult protocol + 4 adapter shells."""

from __future__ import annotations

import pytest

from orchestration.steps import (
    DEFAULT_ADAPTERS,
    EndpointSpec,
    StepContext,
    _redact_payload,
    step_perf,
    step_pricing,
    step_probe,
    step_purity,
)
from security.bearer import CredentialBearer


def _ctx(step: str, *, mock: bool = True, run_params: dict | None = None) -> StepContext:
    return StepContext(
        step=step,
        endpoint=EndpointSpec(
            name="ep-test",
            base_url="https://example.test",
            credential=CredentialBearer("sk-fake-NEVER-A-REAL-KEY-1234567890"),
        ),
        model="model-x",
        run_params={"mock": mock, **(run_params or {})},
    )


# ---- All four shells happy-path -------------------------------------------

@pytest.mark.parametrize(
    "step_name,adapter",
    [
        ("probe", step_probe),
        ("purity", step_purity),
        ("perf", step_perf),
        ("pricing", step_pricing),
    ],
)
def test_shell_mock_path_returns_ok(step_name, adapter):
    res = adapter(_ctx(step_name))
    assert res["status"] == "ok"
    assert res["step"] == step_name
    assert res["endpoint"] == "ep-test"
    assert res["model"] == "model-x"
    assert res["latency_ms"] >= 0
    assert res["payload"]["mock"] is True
    assert res["schema_version"] == 1
    assert isinstance(res["code_version"], str)


# ---- Shells raise NotImplementedError on the non-mock path -----------------

@pytest.mark.parametrize(
    "adapter,step_name",
    [
        (step_probe, "probe"),
        (step_purity, "purity"),
        (step_pricing, "pricing"),
    ],
)
def test_shell_non_mock_path_raises(adapter, step_name):
    ctx = _ctx(step_name, mock=False)
    with pytest.raises(NotImplementedError, match=step_name):
        adapter(ctx)


# step_perf defaults to mock=True even when mock not passed
def test_step_perf_defaults_to_mock():
    ctx = StepContext(
        step="perf",
        endpoint=EndpointSpec(
            name="e", base_url="https://x.test",
            credential=CredentialBearer("sk-fake-DO-NOT-LEAK-1234567890"),
        ),
        model="m",
        run_params={},  # no mock key at all
    )
    res = step_perf(ctx)
    assert res["status"] == "ok"
    assert res["payload"]["mock"] is True


# ---- DEFAULT_ADAPTERS registry --------------------------------------------

def test_default_adapters_covers_four_steps():
    assert set(DEFAULT_ADAPTERS.keys()) == {"probe", "purity", "perf", "pricing"}


def test_default_adapters_are_callable():
    for fn in DEFAULT_ADAPTERS.values():
        assert callable(fn)


# ---- Redaction belt-and-suspenders ----------------------------------------

def test_redact_payload_strips_sk_keys_from_strings():
    p = {"echoed_key": "the key is sk-ABCDEFGHIJ1234567890XYZ"}
    cleaned = _redact_payload(p, sentinel_keys=())
    assert "sk-ABCDEFGHIJ1234567890XYZ" not in cleaned["echoed_key"]


def test_redact_payload_recurses_into_nested_dicts_and_lists():
    p = {
        "nested": {"echo": "Bearer abcdefghijklmnopqrstu"},
        "alist": ["api_key=mysecretsecretlong123456"],
        "untouched": 42,
    }
    cleaned = _redact_payload(p, sentinel_keys=())
    assert "abcdefghijklmnopqrstu" not in cleaned["nested"]["echo"]
    assert "mysecretsecretlong123456" not in cleaned["alist"][0]
    assert cleaned["untouched"] == 42


# ---- Credential never leaks into a returned StepResult --------------------

def test_credential_never_in_payload_after_mock_run():
    secret = "sk-DOXXING-MYSELF-WOULD-BE-BAD-LONGLONG"
    ctx = StepContext(
        step="probe",
        endpoint=EndpointSpec(
            name="e", base_url="https://x.test",
            credential=CredentialBearer(secret),
        ),
        model="m",
        run_params={"mock": True},
    )
    res = step_probe(ctx)
    # Walk the result tree for the secret string anywhere.
    import json
    serialised = json.dumps(res, default=str)
    assert secret not in serialised
