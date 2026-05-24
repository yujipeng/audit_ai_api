"""Credential isolation primitives — design §2.5.2 rules 1–3.

* CredentialBearer — wraps a credential string so repr/str/format/serialization
  never expose it. Stable 8-char id (sha256 prefix) for log correlation.
* safe_subprocess_env — builds a minimal env dict for subprocess.run. The credential
  is exported by env name only — never appended to argv (where it would show up
  in /proc/cmdline and ps output).
* require_explicit_credential_kwargs — function decorator that rejects **kwargs
  swallows around credential-bearing functions and ensures the named credential
  kwarg is present in the signature.
"""

from __future__ import annotations

import hashlib
import inspect
import os
from typing import Callable, Iterable, TypeVar

__all__ = [
    "CredentialBearer",
    "require_explicit_credential_kwargs",
    "safe_subprocess_env",
]

F = TypeVar("F", bound=Callable[..., object])


class CredentialBearer:
    """A credential wrapper that refuses to leak via the usual side-channels.

    Instances expose only `reveal()` (returns plaintext) and `redacted_id`
    (stable sha256[:8]). repr/str/format and any object-serializer return the
    redacted id or refuse outright.
    """

    __slots__ = ("_value", "_id")

    def __init__(self, value: str) -> None:
        if not isinstance(value, str):
            raise TypeError("CredentialBearer requires a str value")
        self._value = value
        self._id = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]

    def reveal(self) -> str:
        return self._value

    @property
    def redacted_id(self) -> str:
        return self._id

    def __repr__(self) -> str:
        return f"<CredentialBearer id={self._id}>"

    def __str__(self) -> str:
        return f"<CredentialBearer id={self._id}>"

    def __format__(self, spec: str) -> str:
        return str(self)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CredentialBearer) and self._value == other._value

    def __hash__(self) -> int:
        return hash(("CredentialBearer", self._value))

    # Object serialization (and copy.deepcopy by extension) is the most common
    # accidental exfiltration path. Refuse it loudly.
    def __reduce__(self):
        raise TypeError("CredentialBearer instances are not serializable")

    def __reduce_ex__(self, protocol):
        raise TypeError("CredentialBearer instances are not serializable")


# Minimum env the child process needs to start. PATH for the loader, LANG/LC_*
# for utf-8 decoding, HOME/USER for tools that look them up, TMPDIR for tempfiles.
_INHERIT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SHELL",
    "SYSTEMROOT",
)


def safe_subprocess_env(
    credentials: dict[str, str | CredentialBearer],
    *,
    extra_inherit: Iterable[str] = (),
) -> dict[str, str]:
    """Build a minimal env dict for subprocess.run.

    Inherits only an allow-listed set of parent env vars. The caller-supplied
    `credentials` map is the only place where secrets enter the env — and they
    enter by name, not by mention in argv.
    """
    env: dict[str, str] = {}
    allow = set(_INHERIT_ENV_ALLOWLIST) | set(extra_inherit)
    for name in allow:
        if name in os.environ:
            env[name] = os.environ[name]
    for name, value in credentials.items():
        if isinstance(value, CredentialBearer):
            env[name] = value.reveal()
        elif isinstance(value, str):
            env[name] = value
        else:
            raise TypeError(
                f"credential {name!r} must be str or CredentialBearer, "
                f"got {type(value).__name__}"
            )
    return env


def require_explicit_credential_kwargs(
    credential_kwargs: Iterable[str],
) -> Callable[[F], F]:
    """Decorator: refuse to wrap a function that swallows **kwargs or omits the
    declared credential kwarg names.

    Forces every credential-bearing call site to spell out the kwarg explicitly,
    which makes static review and grep-based audits possible.
    """
    required = tuple(credential_kwargs)

    def decorator(fn: F) -> F:
        sig = inspect.signature(fn)
        for p in sig.parameters.values():
            if p.kind is inspect.Parameter.VAR_KEYWORD:
                raise TypeError(
                    f"{fn.__name__!r} accepts **kwargs; credential-bearing functions "
                    "must use explicit keyword-only parameters"
                )
        param_names = set(sig.parameters)
        for name in required:
            if name not in param_names:
                raise TypeError(
                    f"{fn.__name__!r} is missing required credential kwarg {name!r}"
                )
        return fn

    return decorator
