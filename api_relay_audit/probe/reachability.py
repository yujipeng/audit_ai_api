"""S1 probe-core / Story-2 (TES-150) — P1 endpoint reachability.

This is the first probe of the four-probe S1 sequence. Its goal is to
answer the cheapest possible "is the endpoint *physically* there?"
question before the audit spends user tokens on P2/P3/P4.

Decision tree (PRD §3.3 / §6.2 A4):

    1. DNS resolves?              no → status=error, code=dns_resolution_failed
    2. httpx GET to root URL?
        - 2xx                       → status=ok
        - 4xx                       → status=degraded (P2 will follow up)
        - 5xx                       → status=error, code=http_5xx
    3. httpx ConnectError?
        - SSL-flavoured            → try curl -sk; on success
                                     → status=degraded, fallback_to_curl=True,
                                       signals += ["reachability:via-curl"]
                                     ; on failure
                                     → status=error, code=tls_handshake_failed
        - Connect-refused          → status=error, code=tcp_refused
    4. httpx TimeoutException     → status=error, code=timeout

Curl fallback rationale (mirrors :mod:`api_relay_audit.client`): a large
share of self-hosted relays present a self-signed or expired TLS cert.
Refusing to probe them at all turns audit.py into a useless tool for the
exact users who need it most. Falling back to ``curl -sk`` for a
read-only GET is the same trade the rest of the suite makes; the
``reachability:via-curl`` signal lets the aggregator demote the verdict
so the user notices.

The module exposes a single public entry, :func:`probe_reachability`.
Tests inject the network seams (``_resolve_host``, ``_httpx_get_root``,
``_curl_get_root``) via ``monkeypatch.setattr`` rather than mocking
httpx at the transport level — this keeps each branch self-contained
and avoids depending on httpx's internal class hierarchy, which is
relatively volatile.
"""

from __future__ import annotations

import json
import socket
import subprocess
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from .types import ProbeError, ReachabilityResult


# ---------------------------------------------------------------------------
# Network seams (overridden by tests via monkeypatch)
# ---------------------------------------------------------------------------


def _resolve_host(host: str) -> bool:
    """Return True iff ``host`` resolves to at least one address.

    Raises ``socket.gaierror`` on failure so the caller can surface the
    underlying message verbatim through ``ProbeError.message``.
    """
    socket.getaddrinfo(host, None)
    return True


def _httpx_get_root(url: str, timeout: float):
    """GET the URL root with httpx, no redirect following.

    We disable redirect following so a relay that 301s ``/`` → ``/docs``
    does not silently turn into a 200 hit; ``http_status_root`` should
    reflect the literal first-hop status.
    """
    return httpx.get(url, timeout=timeout, follow_redirects=False)


def _curl_get_root(url: str, timeout: float):
    """GET the URL root via ``curl -sk`` subprocess.

    Returns an object with a ``status_code`` attribute so the caller can
    treat the result identically to an httpx response. Raises
    ``RuntimeError`` if curl exits non-zero or its output cannot be
    parsed — the caller maps both to ``code=tls_handshake_failed`` since
    we only reach this path on a Python-level SSL error.
    """
    cmd = [
        "curl",
        "-sk",
        "-o",
        "/dev/null",
        "-w",
        "%{http_code}",
        "--max-time",
        str(int(timeout) + 1),
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    if r.returncode != 0:
        raise RuntimeError(f"curl failed (rc={r.returncode}): {r.stderr[:200]}")
    try:
        code = int(r.stdout.strip())
    except (TypeError, ValueError):
        raise RuntimeError(f"curl returned non-integer status: {r.stdout!r}")

    class _CurlResponse:
        status_code = code

    return _CurlResponse()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _root_url(base_url: str) -> Optional[str]:
    """Normalise ``base_url`` to a scheme://host[:port] root (no trailing slash).

    Returns ``None`` if the URL is unparseable or has no scheme/host.
    PRD §3.4 / R4: P1 probes the root, not ``/v1``, because some relays
    return 404 on ``/v1`` but 200 on ``/``.
    """
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    if parsed.scheme not in ("http", "https"):
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_ssl_error(exc: BaseException) -> bool:
    """Return True if the exception message smells like a TLS/SSL failure.

    httpx wraps SSL errors in ``ConnectError`` whose ``__cause__`` is an
    ``ssl.SSLError``; the surface message reliably contains ``"SSL"`` /
    ``"CERTIFICATE"`` / ``"handshake"``. Falling back on those tokens
    is acceptable because the cost of a false-positive curl retry is one
    extra request, and the cost of a false-negative is the entire SSL
    branch never running curl recovery.
    """
    msg = str(exc).lower()
    return any(tok in msg for tok in ("ssl", "certificate", "handshake", "tls"))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def probe_reachability(base_url: str, timeout_s: float = 8.0) -> ReachabilityResult:
    """Run the P1 reachability probe against ``base_url``.

    Parameters
    ----------
    base_url
        Full relay URL (with scheme). Trailing ``/v1`` is stripped before
        the GET — P1 probes the root, not the API path.
    timeout_s
        Per-probe wall-clock cap. Default 8s (PRD §5.2 N1). Used as both
        the httpx connect/read timeout and the upper bound for the curl
        subprocess.

    Returns
    -------
    ReachabilityResult
        Frozen dataclass; ``status`` is one of ``ok`` / ``degraded`` /
        ``error``. The aggregator (Story-6) maps these to the
        overall ``verdict`` and decides whether to short-circuit P2/P3/P4.
    """
    root = _root_url(base_url)
    if root is None:
        return ReachabilityResult(
            status="error",
            signals=["reachability:invalid-base-url"],
            error=ProbeError(
                code="invalid_base_url",
                message=f"base_url is not a parseable http(s) URL: {base_url!r}",
            ),
        )

    parsed = urlparse(root)
    host = parsed.hostname or ""

    # --- Phase 1: DNS ----------------------------------------------------
    try:
        _resolve_host(host)
    except socket.gaierror as exc:
        return ReachabilityResult(
            status="error",
            dns_resolves=False,
            signals=["reachability:dns-failed"],
            error=ProbeError(
                code="dns_resolution_failed",
                message=f"DNS lookup failed for {host}: {exc}",
            ),
        )

    # --- Phase 2: httpx GET ---------------------------------------------
    t0 = time.perf_counter()
    try:
        response = _httpx_get_root(root, timeout=timeout_s)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        return _classify_http_response(
            response.status_code,
            latency_ms=elapsed_ms,
            fallback_to_curl=False,
        )
    except httpx.TimeoutException as exc:
        return ReachabilityResult(
            status="error",
            dns_resolves=True,
            tcp_ok=False,
            tls_ok=False,
            signals=["reachability:timeout"],
            error=ProbeError(
                code="timeout",
                message=f"httpx timeout after {timeout_s}s: {exc}",
            ),
        )
    except httpx.ConnectError as exc:
        ssl_like = _looks_like_ssl_error(exc)
        if not ssl_like:
            # Pure TCP refused — no point retrying with curl.
            return ReachabilityResult(
                status="error",
                dns_resolves=True,
                tcp_ok=False,
                tls_ok=False,
                signals=["reachability:tcp-refused"],
                error=ProbeError(
                    code="tcp_refused",
                    message=f"TCP connect failed: {exc}",
                ),
            )
        # SSL-flavoured ConnectError → try curl -sk recovery.
        return _curl_recovery(root, timeout_s, original_error=exc)


def _classify_http_response(
    status_code: int,
    *,
    latency_ms: int,
    fallback_to_curl: bool,
) -> ReachabilityResult:
    """Map an HTTP status code to the P1 verdict + signals.

    Centralising the table here keeps the httpx and curl branches in
    sync (Story-2 DoD: identical post-classification regardless of
    transport). A response in any branch means we got past TCP+TLS, so
    ``tcp_ok`` / ``tls_ok`` are True unless the caller overrides them.
    """
    signals: list[str] = []
    if fallback_to_curl:
        signals.append("reachability:via-curl")

    if 200 <= status_code < 400:
        return ReachabilityResult(
            status="degraded" if fallback_to_curl else "ok",
            dns_resolves=True,
            tcp_ok=True,
            tls_ok=not fallback_to_curl,
            http_status_root=status_code,
            latency_ms=latency_ms,
            fallback_to_curl=fallback_to_curl,
            signals=signals,
            error=None,
        )
    if 400 <= status_code < 500:
        signals.append("reachability:http-4xx")
        return ReachabilityResult(
            status="degraded",
            dns_resolves=True,
            tcp_ok=True,
            tls_ok=not fallback_to_curl,
            http_status_root=status_code,
            latency_ms=latency_ms,
            fallback_to_curl=fallback_to_curl,
            signals=signals,
            error=None,
        )
    # 5xx — error
    signals.append("reachability:http-5xx")
    return ReachabilityResult(
        status="error",
        dns_resolves=True,
        tcp_ok=True,
        tls_ok=not fallback_to_curl,
        http_status_root=status_code,
        latency_ms=latency_ms,
        fallback_to_curl=fallback_to_curl,
        signals=signals,
        error=ProbeError(
            code="http_5xx",
            message=f"HTTP {status_code} from root URL",
        ),
    )


def _curl_recovery(
    root: str,
    timeout_s: float,
    *,
    original_error: BaseException,
) -> ReachabilityResult:
    """SSL fallback path: attempt ``curl -sk`` and classify.

    Returns ``status=degraded`` on success (with ``reachability:via-curl``
    signal) and ``status=error`` with ``code=tls_handshake_failed`` if
    curl also fails. ``tcp_ok`` is reported as True in both cases since
    a Python-level SSL error implies the socket reached the peer.
    """
    t0 = time.perf_counter()
    try:
        response = _curl_get_root(root, timeout=timeout_s)
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
    except (RuntimeError, subprocess.TimeoutExpired, OSError) as curl_exc:
        return ReachabilityResult(
            status="error",
            dns_resolves=True,
            tcp_ok=True,
            tls_ok=False,
            fallback_to_curl=True,
            signals=["reachability:tls-failed"],
            error=ProbeError(
                code="tls_handshake_failed",
                message=(
                    f"httpx SSL error ({original_error}); "
                    f"curl recovery also failed: {curl_exc}"
                ),
            ),
        )

    return _classify_http_response(
        response.status_code,
        latency_ms=elapsed_ms,
        fallback_to_curl=True,
    )


__all__ = ["probe_reachability"]
