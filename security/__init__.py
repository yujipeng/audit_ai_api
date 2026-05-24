"""Security baseline for the api-relay-audit orchestration layer.

Public surface:
    redact_text, redact_url, redact_response_text  — output sanitization
    CredentialBearer                                — credential wrapper
    safe_subprocess_env                             — argv-free subprocess invocation
    require_explicit_credential_kwargs              — kwargs hygiene decorator
    load_secrets_yaml                               — fail-fast YAML loader

Downstream S5-B/C/D MUST import these primitives at module load — never re-implement
ad-hoc regexes or credential containers. The CI grep gate enforces that the file
`security/redact.py` is the only place where credential patterns live.
"""

from security.bearer import (
    CredentialBearer,
    require_explicit_credential_kwargs,
    safe_subprocess_env,
)
from security.redact import (
    REDACT_PLACEHOLDER,
    redact_response_text,
    redact_text,
    redact_url,
)
from security.secrets import (
    PlaintextCredentialError,
    UndefinedEnvReferenceError,
    load_secrets_yaml,
    resolve_env_references,
)

__all__ = [
    "CredentialBearer",
    "PlaintextCredentialError",
    "REDACT_PLACEHOLDER",
    "UndefinedEnvReferenceError",
    "load_secrets_yaml",
    "redact_response_text",
    "redact_text",
    "redact_url",
    "require_explicit_credential_kwargs",
    "resolve_env_references",
    "safe_subprocess_env",
]
