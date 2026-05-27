"""Pytest conftest — sys.path adapters for hyphenated CLI scripts.

Makes ``scripts/purity-eval.py`` importable as ``purity_eval`` via
``importlib.util`` (so tests/test_purity_cli.py can call the pure
``_compute_exit_code`` helper directly without spawning subprocesses).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _load_purity_eval_module() -> None:
    if "purity_eval" in sys.modules:
        return
    target = SCRIPTS_DIR / "purity-eval.py"
    if not target.exists():
        return
    spec = importlib.util.spec_from_file_location("purity_eval", target)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["purity_eval"] = module
    spec.loader.exec_module(module)


_load_purity_eval_module()
