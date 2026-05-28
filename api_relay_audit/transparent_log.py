"""Append-only JSONL forensic logger (arXiv:2604.08407 §7.3).

Records every API request made during an audit run with timestamp,
URL, SHA-256 of request/response bytes, status code, response
headers, and transport metadata. **Hash only, not body** — keeps
entries <=1.5 KB and avoids credential-at-rest risk.

TLS metadata capture is deferred to a follow-up commit; the
``tls_version`` and ``tls_cipher`` fields are always ``null`` for now.
"""

import hashlib
import json
import os
import sys
from datetime import datetime, timezone


def redact_error(error):
    """Strip response body content from error strings for safe logging.

    Error strings like ``"HTTP 400: {body[:200]}"`` or
    ``"curl failed: {stderr[:200]}"`` may contain sensitive content
    (leaked API keys, upstream URLs). This function keeps only the
    error type and HTTP status, discarding everything after the first
    colon. Other errors (exception messages, timeouts) pass through
    unchanged.

    Returns:
        Redacted error string, or ``None`` if input is ``None``.
    """
    if error is None:
        return None
    for prefix in ("HTTP ", "curl failed"):
        if error.startswith(prefix):
            colon = error.find(":")
            if colon != -1:
                return error[:colon]
            return error
    return error


def sha256hex(data):
    """Return the SHA-256 hex digest of *data* (bytes or str).

    ``None`` input returns ``None`` (e.g. when the response body
    is not available due to a transport error).
    """
    if data is None:
        return None
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


class TransparentLogger:
    """Append-only JSONL writer for forensic request logging.

    Each call to :meth:`log_entry` writes one JSON line and flushes
    immediately for crash-safety. Never raises — I/O errors are
    printed to stderr so the audit is not interrupted by a full disk
    or read-only path.

    Usage::

        logger = TransparentLogger("/tmp/audit.jsonl")
        logger.log_entry({"timestamp": "...", "url": "..."})
        logger.close()
    """

    def __init__(self, path: str):
        self._path = path
        # Create parent directories if they don't exist (MEDIUM fix).
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._f = open(path, "a", encoding="utf-8")

    def log_entry(self, entry: dict) -> None:
        """Serialise *entry* as a single JSON line and flush."""
        try:
            self._f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            self._f.flush()
        except Exception as e:
            print(f"  [transparent-log] write error: {e}", file=sys.stderr)

    def log_pricing_entry(self, sample: dict, aggregated) -> None:
        """Append a pricing-aggregate entry (Story TES-137 / S4-D, PRD §6.5.2).

        Writes a JSONL line with namespaced ``pricing.*`` keys so the
        operator can grep the forensic log without parsing nested objects.
        ``balance_drift`` is sourced from any layer that recorded a
        ``balance_drift_pct`` detail (typically L2); absent when no layer
        observed balance data — the audit must not synthesise a zero.
        """
        balance_drift = None
        # Walk highest-confidence layer first (L2 → L1 → L0); the
        # balance signal is owned by L2, so we never let an L0/L1
        # incidental ``balance_drift_pct`` shadow it.
        for layer in ("L2_balance_triangle", "L1_tokenizer",
                      "L0_character_ratio"):
            details = aggregated.per_layer.get(layer)
            if details and "balance_drift_pct" in details:
                balance_drift = details["balance_drift_pct"]
                break

        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "method": "pricing",
            "sample": dict(sample) if sample else {},
            "pricing.severity": aggregated.severity.value,
            "pricing.layer": aggregated.layer,
            "pricing.confidence": aggregated.confidence,
            "pricing.token_drift": aggregated.drift_pct,
            "pricing.balance_drift": balance_drift,
            "pricing.suppressed_by": list(aggregated.suppressed_by),
            "pricing.per_layer": {
                k: dict(v) for k, v in aggregated.per_layer.items()
            },
        }
        self.log_entry(entry)

    def close(self) -> None:
        """Close the underlying file handle (idempotent)."""
        try:
            self._f.close()
        except Exception:
            pass
