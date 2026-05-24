"""Tests for orchestration.config — audit-suite v1 + legacy perf-bench adapter."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from orchestration.config import (
    CONFIG_SCHEMA_VERSION,
    OrchestrationConfig,
    compute_config_digest,
    load_config,
)
from security.bearer import CredentialBearer


# ---- v1 schema -------------------------------------------------------------

def _write_v1(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "audit-suite.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_v1_minimal(tmp_path: Path):
    p = _write_v1(tmp_path, """
schema_version: 1
run:
  steps: [probe, perf]
  concurrency: 2
  mock: true
endpoints:
  - name: ep-a
    base_url: https://relay.example.com/v1
    credential: sk-fake-NOT-A-REAL-KEY-1234567890
    vendor: claude
    models: [claude-haiku-4-5]
params:
  perf:
    rounds: 3
""")
    cfg = load_config(p)
    assert isinstance(cfg, OrchestrationConfig)
    assert cfg.run.steps == ("probe", "perf")
    assert cfg.run.concurrency == 2
    assert cfg.run.mock is True
    assert len(cfg.endpoints) == 1
    assert cfg.endpoints[0].name == "ep-a"
    assert isinstance(cfg.endpoints[0].credential, CredentialBearer)
    assert cfg.params["perf"]["rounds"] == 3
    assert len(cfg.config_digest) == 64  # sha256 hex


def test_load_v1_rejects_unknown_step(tmp_path: Path):
    p = _write_v1(tmp_path, """
schema_version: 1
run:
  steps: [probe, NOT_A_STEP]
endpoints:
  - base_url: https://x.test
    credential: sk-fake-NOT-REAL-KEY-1234567890
""")
    with pytest.raises(ValueError, match="unknown step"):
        load_config(p)


def test_load_v1_rejects_wrong_schema_version(tmp_path: Path):
    p = _write_v1(tmp_path, """
schema_version: 99
run: {steps: [probe]}
endpoints:
  - base_url: https://x.test
    credential: sk-fake-NOT-REAL-KEY-1234567890
""")
    with pytest.raises(ValueError, match="schema_version"):
        load_config(p)


def test_load_v1_resolves_env_references(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "sk-fromenv-NOT-A-REAL-KEY-1234567890")
    p = _write_v1(tmp_path, """
schema_version: 1
run: {steps: [probe], mock: true}
endpoints:
  - base_url: https://x.test
    credential: $ENV{MY_TEST_KEY}
""")
    cfg = load_config(p)
    assert cfg.endpoints[0].credential.reveal() == "sk-fromenv-NOT-A-REAL-KEY-1234567890"


def test_load_v1_undefined_env_reference_raises(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DEFINITELY_NOT_SET_XYZ", raising=False)
    p = _write_v1(tmp_path, """
schema_version: 1
run: {steps: [probe]}
endpoints:
  - base_url: https://x.test
    credential: $ENV{DEFINITELY_NOT_SET_XYZ}
""")
    with pytest.raises(Exception):
        load_config(p)


# ---- Legacy perf-bench adapter --------------------------------------------

def test_load_legacy_perf_bench_yaml(tmp_path: Path):
    p = tmp_path / "perf.yaml"
    p.write_text("""
test:
  prompt: "ping"
  rounds: 2
  concurrency: 4
  format: openai
default_models: [m1, m2]
endpoints:
  - name: ep-legacy
    base_url: https://relay.example.com
    api_key: sk-legacy-NOT-A-REAL-KEY-1234567890
    vendor: gpt
""", encoding="utf-8")
    cfg = load_config(p)
    assert cfg.run.steps == ("perf",)
    assert cfg.run.concurrency == 4
    assert cfg.endpoints[0].name == "ep-legacy"
    assert cfg.params["perf"]["rounds"] == 2
    assert cfg.params["perf"]["prompt"] == "ping"


def test_load_unknown_format_rejected(tmp_path: Path):
    p = tmp_path / "broken.yaml"
    p.write_text("just: a-string", encoding="utf-8")
    with pytest.raises(ValueError, match="unrecognised config"):
        load_config(p)


# ---- Config digest --------------------------------------------------------

def test_config_digest_changes_when_steps_change(tmp_path: Path):
    body_a = """
schema_version: 1
run: {steps: [probe], mock: true}
endpoints: [{base_url: https://x.test, credential: sk-fake-NOT-REAL-KEY-1234567890}]
"""
    body_b = body_a.replace("[probe]", "[probe, purity]")
    a = _write_v1(tmp_path, body_a)
    b = tmp_path / "b.yaml"
    b.write_text(body_b, encoding="utf-8")
    cfg_a = load_config(a)
    cfg_b = load_config(b)
    assert cfg_a.config_digest != cfg_b.config_digest


def test_config_digest_excludes_credential_plaintext(tmp_path: Path):
    body_template = """
schema_version: 1
run: {{steps: [probe], mock: true}}
endpoints:
  - base_url: https://x.test
    credential: {cred}
"""
    a = _write_v1(tmp_path, body_template.format(cred="sk-aaaaaaaaaaaaaaaaaaaaaa"))
    b = tmp_path / "b.yaml"
    b.write_text(body_template.format(cred="sk-bbbbbbbbbbbbbbbbbbbbbb"), encoding="utf-8")
    cfg_a = load_config(a)
    cfg_b = load_config(b)
    # Same structural inputs → same digest, even with different credentials.
    assert cfg_a.config_digest == cfg_b.config_digest
