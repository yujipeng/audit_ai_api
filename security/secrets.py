"""Fail-fast YAML credential loader.

Two failure modes both fire at load time, both surface the YAML line number:

  PlaintextCredentialError       — A YAML mapping value looks like a real key
                                   (matches one of the SK/Bearer/header patterns
                                   from security.redact). The fix is to move
                                   the value into env and reference it via
                                   `$ENV{VAR_NAME}`.

  UndefinedEnvReferenceError     — A `$ENV{VAR}` reference resolves to nothing
                                   because VAR is not set in the process env.

The loader uses a PyYAML SafeLoader subclass that stamps the source line number
onto every mapping node, then walks the parsed tree to detect both conditions
before returning.
"""

from __future__ import annotations

import os
import re
from typing import Any

import yaml

from security.bearer import CredentialBearer
from security.redact import BEARER_PATTERN, HEADER_PATTERN, SK_PATTERN

__all__ = [
    "PlaintextCredentialError",
    "UndefinedEnvReferenceError",
    "load_secrets_yaml",
    "resolve_env_references",
]


_ENV_REF = re.compile(r"\$ENV\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PlaintextCredentialError(ValueError):
    """Raised when a credential value appears in YAML as plaintext."""


class UndefinedEnvReferenceError(ValueError):
    """Raised when a `$ENV{VAR}` reference resolves to nothing."""


class _LineLoader(yaml.SafeLoader):
    """SafeLoader subclass that records the source line number on every node."""


def _construct_mapping_with_lines(loader: _LineLoader, node: yaml.MappingNode):
    mapping: dict[Any, Any] = {}
    line_map: dict[Any, int] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        value = loader.construct_object(value_node, deep=True)
        mapping[key] = value
        # PyYAML line indices are 0-based; humans count from 1.
        line_map[key] = value_node.start_mark.line + 1
    mapping["__lines__"] = line_map
    return mapping


_LineLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_mapping_with_lines,
)


def resolve_env_references(value: str, *, line: int | None = None) -> str:
    """Replace every `$ENV{VAR}` token in `value` with os.environ[VAR].

    Raises UndefinedEnvReferenceError if any referenced var is missing.
    `line` is included in the error message when provided.
    """

    def _replace(m: re.Match[str]) -> str:
        var = m.group(1)
        if var not in os.environ:
            where = f" (line {line})" if line is not None else ""
            raise UndefinedEnvReferenceError(
                f"$ENV{{{var}}}{where} is not set in the environment"
            )
        return os.environ[var]

    return _ENV_REF.sub(_replace, value)


def _looks_like_plaintext_credential(value: str) -> bool:
    """Detect values that look like real credentials and should never appear
    in YAML as plaintext.

    The same patterns that drive `redact_text` are reused here so the rule
    "if redact would catch it, fail-fast" stays in sync automatically."""
    if not isinstance(value, str):
        return False
    if _ENV_REF.search(value):
        return False  # explicit env reference is the sanctioned form
    if SK_PATTERN.search(value):
        return True
    if BEARER_PATTERN.search(value):
        return True
    # HEADER_PATTERN expects key=value form; for bare string YAML values we
    # additionally treat any 20+ char "secret-looking" string under a
    # known-sensitive key as suspicious. The walker below handles the
    # key-name-driven check.
    return False


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "secret",
        "secret_key",
        "token",
        "auth",
        "authorization",
        "x_api_key",
        "x-api-key",
        "header",  # e.g. `header: 'Bearer ...'`
    }
)


def _walk_for_plaintext(
    node: Any,
    *,
    path: tuple[str, ...] = (),
    parent_line_map: dict[Any, int] | None = None,
) -> None:
    if isinstance(node, dict):
        lines = node.get("__lines__", {})
        for k, v in node.items():
            if k == "__lines__":
                continue
            line = lines.get(k)
            new_path = path + (str(k),)
            if isinstance(v, str):
                key_name = str(k).lower()
                is_sensitive_key = key_name in _SENSITIVE_KEY_NAMES
                if _looks_like_plaintext_credential(v) or (
                    is_sensitive_key and not _ENV_REF.search(v) and len(v) >= 20
                ):
                    raise PlaintextCredentialError(
                        f"plaintext credential at {'.'.join(new_path)} "
                        f"(line {line}); move the value to env and reference "
                        f"it as $ENV{{VAR_NAME}}"
                    )
            else:
                _walk_for_plaintext(v, path=new_path, parent_line_map=lines)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_for_plaintext(item, path=path + (f"[{i}]",))


def _resolve_env_in_tree(
    node: Any,
    *,
    wrap: bool,
    line_map: dict[Any, int] | None = None,
) -> Any:
    if isinstance(node, dict):
        lines = node.get("__lines__", {})
        out: dict[Any, Any] = {}
        for k, v in node.items():
            if k == "__lines__":
                continue
            out[k] = _resolve_env_in_tree(v, wrap=wrap, line_map={k: lines.get(k)})
        return out
    if isinstance(node, list):
        return [_resolve_env_in_tree(item, wrap=wrap) for item in node]
    if isinstance(node, str) and _ENV_REF.search(node):
        line = None
        if line_map:
            line = next(iter(line_map.values()), None)
        resolved = resolve_env_references(node, line=line)
        if wrap:
            return CredentialBearer(resolved)
        return resolved
    return node


def load_secrets_yaml(path: str, *, wrap_credentials: bool = False) -> dict[str, Any]:
    """Load a YAML config file, failing fast on plaintext credentials and
    undefined env references.

    Args:
        path: filesystem path to the YAML file.
        wrap_credentials: if True, every resolved `$ENV{VAR}` value is wrapped
            in a CredentialBearer so it cannot leak through accidental logging.

    Returns:
        The parsed config tree with `$ENV{VAR}` references resolved (and
        wrapped if requested). The `__lines__` bookkeeping keys are stripped.
    """
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.load(f, Loader=_LineLoader)
    if raw is None:
        return {}

    _walk_for_plaintext(raw)
    return _resolve_env_in_tree(raw, wrap=wrap_credentials)
