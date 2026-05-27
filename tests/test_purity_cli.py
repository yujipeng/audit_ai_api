"""Red-phase tests for scripts/purity-eval.py.

S2-4 切片 C：CLI flag 全集 + exit code 三档 + AC-R3 重放幂等。
- AC-F1: graceful 跑完，不抛未捕获异常
- AC-F2: JSON schema_version + dimensions keys + overall.verdict 字段命中
- AC-X1: clean → exit 0
- AC-X2: injected | failed → exit 1
- AC-X3: suspicious | unknown 默认 exit 0；--strict → exit 2
- AC-R3: 同 stub 跑两次 JSON 除 metadata 时间戳外字节级一致
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = REPO_ROOT / "scripts" / "purity-eval.py"


def _run_cli(args: list[str], env: dict | None = None) -> subprocess.CompletedProcess:
    cmd = [sys.executable, str(CLI_PATH), *args]
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
    )


class TestCLIHelp:
    """AC-F1：CLI flag 全集。"""

    def test_help_exits_zero(self):
        result = _run_cli(["--help"])
        assert result.returncode == 0, result.stderr

    def test_help_lists_all_flags(self):
        result = _run_cli(["--help"])
        text = result.stdout + result.stderr
        for flag in [
            "--vendor",
            "--rounds",
            "--only",
            "--include",
            "--strict",
            "--baseline",
            "--raw-keep",
            "--output",
            "--json",
            "--html",
        ]:
            assert flag in text, f"missing flag {flag} in --help"
        # Positional
        assert "target" in text
        assert "api_key" in text


class TestExitCodeFunction:
    """AC-X1/X2/X3：纯函数 _compute_exit_code 单测。"""

    def test_clean_exit_zero(self):
        from purity_eval import _compute_exit_code  # type: ignore

        assert _compute_exit_code("clean", strict=False) == 0
        assert _compute_exit_code("clean", strict=True) == 0

    def test_injected_exit_one(self):
        from purity_eval import _compute_exit_code  # type: ignore

        assert _compute_exit_code("injected", strict=False) == 1
        assert _compute_exit_code("injected", strict=True) == 1

    def test_failed_exit_one(self):
        from purity_eval import _compute_exit_code  # type: ignore

        assert _compute_exit_code("failed", strict=False) == 1
        assert _compute_exit_code("failed", strict=True) == 1

    def test_suspicious_default_zero_strict_two(self):
        from purity_eval import _compute_exit_code  # type: ignore

        assert _compute_exit_code("suspicious", strict=False) == 0
        assert _compute_exit_code("suspicious", strict=True) == 2

    def test_unknown_default_zero_strict_two(self):
        from purity_eval import _compute_exit_code  # type: ignore

        assert _compute_exit_code("unknown", strict=False) == 0
        assert _compute_exit_code("unknown", strict=True) == 2


class TestCLIEndToEndStub:
    """AC-F1 + AC-F2 + AC-X*：用 stub pipeline 跑 CLI 主流程。"""

    @pytest.fixture
    def stub_env(self, monkeypatch, tmp_path):
        # CLI side-loads stub via PURITY_EVAL_STUB_VERDICT env var.
        # 实现 (切片 D) 见 scripts/purity-eval.py 的 _maybe_install_stub() hook.
        env = dict(__import__("os").environ)
        env["PURITY_EVAL_STUB_VERDICT"] = "clean"
        return env

    def test_clean_run_writes_json_and_exits_zero(self, tmp_path):
        import os

        out = tmp_path / "p.json"
        env = dict(os.environ)
        env["PURITY_EVAL_STUB_VERDICT"] = "clean"
        result = _run_cli(
            [
                "https://example.com",
                "sk-fake",
                "claude",
                "--rounds",
                "1",
                "--output",
                str(out),
                "--json",
            ],
            env=env,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        assert out.exists(), f"json output not written: {result.stdout}"
        data = json.loads(out.read_text())
        assert data["schema_version"] == "purity-v2.0"
        assert data["overall"]["verdict"] == "clean"

    def test_html_output_has_six_sections(self, tmp_path):
        import os

        out = tmp_path / "p.html"
        env = dict(os.environ)
        env["PURITY_EVAL_STUB_VERDICT"] = "clean"
        result = _run_cli(
            [
                "https://example.com",
                "sk-fake",
                "claude",
                "--rounds",
                "1",
                "--output",
                str(out),
                "--html",
            ],
            env=env,
        )
        assert result.returncode == 0, result.stderr + result.stdout
        html_text = out.read_text()
        for dim in ("injection", "swap", "drift", "tool", "refusal", "leak"):
            assert f'data-dim="{dim}"' in html_text, f"missing section {dim}"

    def test_injected_exits_one(self, tmp_path):
        import os

        out = tmp_path / "p.json"
        env = dict(os.environ)
        env["PURITY_EVAL_STUB_VERDICT"] = "injected"
        result = _run_cli(
            [
                "https://example.com",
                "sk-fake",
                "claude",
                "--rounds",
                "1",
                "--output",
                str(out),
                "--json",
            ],
            env=env,
        )
        assert result.returncode == 1, result.stderr + result.stdout

    def test_suspicious_default_zero_strict_two(self, tmp_path):
        import os

        out = tmp_path / "p.json"
        env = dict(os.environ)
        env["PURITY_EVAL_STUB_VERDICT"] = "suspicious"
        # default
        result = _run_cli(
            [
                "https://example.com",
                "sk-fake",
                "claude",
                "--rounds",
                "1",
                "--output",
                str(out),
                "--json",
            ],
            env=env,
        )
        assert result.returncode == 0, result.stdout
        # strict
        result = _run_cli(
            [
                "https://example.com",
                "sk-fake",
                "claude",
                "--rounds",
                "1",
                "--output",
                str(out),
                "--json",
                "--strict",
            ],
            env=env,
        )
        assert result.returncode == 2, result.stdout

    def test_replay_idempotent_ac_r3(self, tmp_path):
        """AC-R3: same stub run twice → JSON diff (excluding metadata) = 0 lines."""
        import os

        out1 = tmp_path / "run1.json"
        out2 = tmp_path / "run2.json"
        env = dict(os.environ)
        env["PURITY_EVAL_STUB_VERDICT"] = "clean"
        for out in (out1, out2):
            result = _run_cli(
                [
                    "https://example.com",
                    "sk-fake",
                    "claude",
                    "--rounds",
                    "1",
                    "--output",
                    str(out),
                    "--json",
                ],
                env=env,
            )
            assert result.returncode == 0

        d1 = json.loads(out1.read_text())
        d2 = json.loads(out2.read_text())
        for d in (d1, d2):
            for k in ("started_at", "duration_s", "evaluators_run"):
                d["metadata"].pop(k, None)
            for dim_data in d.get("dimensions", {}).values():
                dim_data.pop("duration_s", None)
        assert d1 == d2
