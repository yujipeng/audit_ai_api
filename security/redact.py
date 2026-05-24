"""Credential redaction — the single source of truth for credential regex patterns.

The four credential pattern families:
    1. SK_PATTERN       — OpenAI / Anthropic style keys: sk-..., sk-ant-..., sk-proj-...
    2. BEARER_PATTERN   — Authorization: Bearer <token>
    3. HEADER_PATTERN   — api_key / x-api-key / access_token assigned in headers or config
    4. QUERY_PATTERN    — ?api_key=... in URLs

`redact_response_text(text, sentinel_keys=...)` additionally strips literal
sentinel substrings; this is the only defense against "the model echoes back
your API key in its response text", which the regex families cannot fully cover
when the model emits the key in obfuscated form.

`redact_url` parses URLs and:
    * strips the entire userinfo (`user:pass@host`),
    * redacts known sensitive query params by name,
    * leaves the rest of the URL intact.

The 20-character minimum match length is calibrated so we do not redact short
debug strings like `sk-fake` while still catching every realistic key. The
threshold matches the CI grep gate (`sk-[A-Za-z0-9]{20,}`).
"""

from __future__ import annotations

import re
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

REDACT_PLACEHOLDER = "[REDACTED]"

_MIN_KEY_LEN = 20

# 1. sk-prefixed keys (OpenAI sk-, sk-proj-, Anthropic sk-ant-). Greedy on the
#    suffix; the {20,} floor keeps us off short debug strings.
SK_PATTERN = re.compile(r"sk-(?:[A-Za-z0-9_\-]+-)*[A-Za-z0-9_\-]{20,}")

# 2. Authorization: Bearer <token> — token is opaque, allow common JWT chars.
BEARER_PATTERN = re.compile(
    r"(?i)(?P<prefix>Bearer\s+)(?P<token>[A-Za-z0-9._\-]{20,})"
)

# 3. Header / config assignment: api_key=..., x-api-key: ..., access_token: "..."
HEADER_PATTERN = re.compile(
    r"(?i)(?P<key>(?:x[-_]?)?(?:api[_-]?key|access[_-]?token|secret[_-]?key))"
    r"(?P<sep>\s*[:=]\s*[\"']?)"
    r"(?P<val>[A-Za-z0-9._\-]{20,})"
)

_SENSITIVE_QUERY_PARAMS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "accesstoken",
        "secret",
        "secret_key",
        "key",
        "token",
        "auth",
        "authorization",
        "x_api_key",
        "x-api-key",
    }
)


def redact_text(s: str) -> str:
    """Redact credential-looking substrings from arbitrary text.

    Order matters: HEADER_PATTERN runs first so it can capture both the label
    and the value (preserving "api_key=" for readability), then BEARER_PATTERN,
    then the loose SK_PATTERN sweep.
    """
    if not s:
        return s

    s = HEADER_PATTERN.sub(
        lambda m: f"{m.group('key')}{m.group('sep')}{REDACT_PLACEHOLDER}", s
    )
    s = BEARER_PATTERN.sub(lambda m: f"{m.group('prefix')}{REDACT_PLACEHOLDER}", s)
    s = SK_PATTERN.sub(REDACT_PLACEHOLDER, s)
    return s


def redact_url(url: str) -> str:
    """Redact credentials embedded in URLs.

    Handles three exposure surfaces:
        * userinfo (`https://user:secret@host`) — stripped entirely
        * sensitive query params — value replaced with the placeholder
        * sk-style keys appearing anywhere in the query — pattern-redacted
    """
    if not url:
        return url

    try:
        parts = urlsplit(url)
    except ValueError:
        # Not a parseable URL — fall back to text redaction.
        return redact_text(url)

    # Strip userinfo.
    netloc = parts.hostname or ""
    if parts.port:
        netloc = f"{netloc}:{parts.port}"

    # Redact sensitive query params, preserve order, and keep unknown ones intact.
    redacted_query_pairs: list[tuple[str, str]] = []
    for k, v in parse_qsl(parts.query, keep_blank_values=True):
        if k.lower() in _SENSITIVE_QUERY_PARAMS:
            redacted_query_pairs.append((k, REDACT_PLACEHOLDER))
        else:
            redacted_query_pairs.append((k, v))
    new_query = urlencode(redacted_query_pairs)
    # Also run sk-pattern over the query string to catch sk-keys appearing as
    # values of non-canonical param names.
    new_query = SK_PATTERN.sub(REDACT_PLACEHOLDER, new_query)

    return urlunsplit((parts.scheme, netloc, parts.path, new_query, parts.fragment))


def redact_response_text(text: str, sentinel_keys: Iterable[str]) -> str:
    """Redact a model's response body before it is rendered into any artifact.

    First strips literal sentinel substrings (the run's keys + a window of
    each), then applies the standard pattern redactors. The literal-strip step
    is what defends against models that echo back the API key verbatim — the
    pattern regexes alone are insufficient because user-supplied custom keys
    may not match the `sk-` prefix.
    """
    if not text:
        return text

    redacted = text
    for raw in sentinel_keys:
        if not raw:
            continue
        # Direct replace.
        if raw in redacted:
            redacted = redacted.replace(raw, REDACT_PLACEHOLDER)
        # Catch partial windows of ≥ 20 chars. Iterate sliding windows from
        # longest down so we replace the largest contiguous run first.
        if len(raw) >= 20:
            for size in range(len(raw), 19, -1):
                # Stop once no further long substrings of the sentinel remain.
                if not any(raw[i : i + size] in redacted for i in range(len(raw) - size + 1)):
                    continue
                for i in range(len(raw) - size + 1):
                    chunk = raw[i : i + size]
                    if chunk in redacted:
                        redacted = redacted.replace(chunk, REDACT_PLACEHOLDER)

    return redact_text(redacted)
