"""Dual-distribution invariant regression test.

The repo ships two parallel versions of the audit tool:

    - ``scripts/audit.py`` (modular, uses ``api_relay_audit/*.py``)
    - ``audit.py`` at repo root (standalone, zero-dep, curl-only)

Any change to one must be mirrored into the other. This test slices the
risk-matrix block from both files and asserts they are character-identical
so that drift is caught immediately.
"""

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def _extract_risk_matrix(path: Path) -> str:
    """Slice the risk-matrix block between the ``# Overall rating`` comment
    and the following ``# Output`` comment. Both files MUST contain both
    markers, otherwise the test is broken and should fail loudly.
    """
    text = path.read_text(encoding="utf-8")
    start_marker = "    # Overall rating\n"
    end_marker = "    # Output\n"
    start = text.find(start_marker)
    end = text.find(end_marker, start)
    if start == -1:
        raise AssertionError(f"Could not find '# Overall rating' marker in {path}")
    if end == -1:
        raise AssertionError(f"Could not find '# Output' marker in {path}")
    return text[start:end]


def test_risk_matrix_character_identical():
    """Regression: the risk matrix code in scripts/audit.py and audit.py MUST
    be character-identical. If this test fails, one of the two was updated
    without the other and the dual-distribution invariant is broken.
    """
    modular = _extract_risk_matrix(REPO_ROOT / "scripts" / "audit.py")
    standalone = _extract_risk_matrix(REPO_ROOT / "audit.py")
    assert modular == standalone, (
        "Risk matrix drift between scripts/audit.py and audit.py. "
        "Update both files so they are character-identical."
    )


def _load_standalone_audit():
    """Load the standalone audit.py as a module so tests can assert against
    its internal constants and helpers."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_standalone_audit_for_parity",
        REPO_ROOT / "audit.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_identity_keywords_standalone_parity():
    """Regression (v1.7.6): the non-Claude identity keyword tuple and the
    strict-keyword frozenset in the standalone audit.py must match the
    modular api_relay_audit/identity_patterns.py. Protects against drift
    on the identity-detection block, which is dual-distributed but not
    covered by the risk-matrix parity test above.
    """
    from api_relay_audit.identity_patterns import (
        NON_CLAUDE_IDENTITY_KEYWORDS as MODULAR_KEYWORDS,
    )
    from api_relay_audit.identity_patterns import (
        _STRICT_ASCII_KEYWORDS as MODULAR_STRICT,
    )

    standalone = _load_standalone_audit()

    assert standalone.NON_CLAUDE_IDENTITY_KEYWORDS == MODULAR_KEYWORDS, (
        "Identity keyword tuple drift between api_relay_audit/identity_patterns.py "
        "and standalone audit.py. Mirror the change into both."
    )
    assert standalone._NON_CLAUDE_STRICT_KEYWORDS == MODULAR_STRICT, (
        "Strict-keyword frozenset drift between identity_patterns.py and "
        "standalone audit.py. Mirror the change into both."
    )


def test_warp_windsurf_present_in_standalone():
    """Regression (v1.7.6→v1.7.7): warp + windsurf must be present AND
    context-strict in standalone audit.py (common English words requiring
    anchor + post-keyword identity signal)."""
    standalone = _load_standalone_audit()
    for kw in ("warp", "windsurf"):
        assert kw in standalone.NON_CLAUDE_IDENTITY_KEYWORDS, (
            f"{kw!r} missing from standalone audit.py"
        )
        assert kw in standalone._NON_CLAUDE_CONTEXT_STRICT_KEYWORDS, (
            f"{kw!r} must be context-strict in standalone audit.py"
        )


def test_standalone_find_non_claude_identities_behaves_like_modular():
    """End-to-end parity: identical inputs must yield identical outputs from
    both distributions' identity-matching functions on v1.7.6 probes."""
    from api_relay_audit.identity_patterns import find_non_claude_identities as modular_fn

    standalone = _load_standalone_audit()
    standalone_fn = standalone.find_non_claude_identities

    probes = [
        "I am Warp, a coding assistant.",
        "I'm Windsurf, an AI IDE.",
        "Engage warp speed.",
        "My hobby is windsurf.",
        "I am Claude, made by Anthropic. Tools like Warp and Windsurf are alternatives.",
    ]
    for text in probes:
        assert modular_fn(text) == standalone_fn(text), (
            f"Divergent identity-match output for probe: {text!r}"
        )


# ---------------------------------------------------------------------------
# Step 12 / Step 13 (v1.8) constants parity
# ---------------------------------------------------------------------------

def test_infra_fingerprint_constants_parity():
    """Regression (v1.8, Codex LOW finding 2026-04-18): Step 12
    fingerprinting constants must match between the modular and
    standalone distributions. Changing a signal, a precedence order,
    or the body scan cap on one side without the other would silently
    bifurcate detection behavior.
    """
    from api_relay_audit.infra_fingerprint import (
        FRAMEWORK_SIGNATURES as MODULAR_SIGS,
        INFORMATIVE_HEADERS as MODULAR_HEADERS,
        _BODY_SCAN_LIMIT as MODULAR_LIMIT,
    )

    standalone = _load_standalone_audit()

    assert standalone.FRAMEWORK_SIGNATURES == MODULAR_SIGS, (
        "FRAMEWORK_SIGNATURES drift between api_relay_audit/infra_fingerprint.py "
        "and standalone audit.py. Mirror the change into both -- signal order "
        "matters (specific frameworks before generic ones)."
    )
    assert standalone.INFORMATIVE_HEADERS == MODULAR_HEADERS, (
        "INFORMATIVE_HEADERS drift between infra_fingerprint.py and standalone "
        "audit.py. These headers are surfaced in the report for 'unknown' "
        "classifications too; divergence leads to asymmetric reports."
    )
    assert standalone._BODY_SCAN_LIMIT == MODULAR_LIMIT, (
        "_BODY_SCAN_LIMIT drift between infra_fingerprint.py and standalone "
        "audit.py. Divergence would change detection on large landing pages."
    )


def test_latency_variance_constants_parity():
    """Regression (v1.8, Codex LOW finding 2026-04-18): Step 13
    latency-variance thresholds must match between the modular and
    standalone distributions. A one-sided change to BIMODAL_GAP_THRESHOLD
    or the CV cutoffs would silently produce different verdicts for
    the same latency data depending on which distribution a user
    installed.
    """
    from api_relay_audit.latency_variance import (
        BIMODAL_GAP_THRESHOLD as MODULAR_BIMODAL,
        CV_STABLE_CUTOFF as MODULAR_STABLE,
        CV_VARIABLE_CUTOFF as MODULAR_VARIABLE,
        DEFAULT_PROBE_COUNT as MODULAR_PROBE_COUNT,
        LATENCY_PROBE_MAX as MODULAR_PROBE_MAX,
        LATENCY_PROBE_MIN as MODULAR_PROBE_MIN,
    )

    standalone = _load_standalone_audit()

    assert standalone.BIMODAL_GAP_THRESHOLD == MODULAR_BIMODAL, (
        "BIMODAL_GAP_THRESHOLD drift between latency_variance.py and "
        "standalone audit.py."
    )
    assert standalone.CV_STABLE_CUTOFF == MODULAR_STABLE, (
        "CV_STABLE_CUTOFF drift between latency_variance.py and "
        "standalone audit.py."
    )
    assert standalone.CV_VARIABLE_CUTOFF == MODULAR_VARIABLE, (
        "CV_VARIABLE_CUTOFF drift between latency_variance.py and "
        "standalone audit.py."
    )
    assert standalone.DEFAULT_PROBE_COUNT == MODULAR_PROBE_COUNT, (
        "DEFAULT_PROBE_COUNT drift between latency_variance.py and "
        "standalone audit.py."
    )
    # v1.8.1 Codex review #5 fix: --latency-probe-count CLI bounds
    # must match across distributions, otherwise a value accepted on
    # one side (e.g. N=60 on modular) would be rejected on the other
    # and documented help text would lie.
    assert standalone.LATENCY_PROBE_MIN == MODULAR_PROBE_MIN, (
        "LATENCY_PROBE_MIN drift between latency_variance.py and "
        "standalone audit.py. CLI bounds must match."
    )
    assert standalone.LATENCY_PROBE_MAX == MODULAR_PROBE_MAX, (
        "LATENCY_PROBE_MAX drift between latency_variance.py and "
        "standalone audit.py. CLI bounds must match."
    )


def test_standalone_uses_perf_counter_not_wall_clock(monkeypatch):
    """v1.8.1 Codex review cycle #2 follow-up: parity regression on the
    clock source.

    The modular side is guarded by
    ``tests/test_latency_variance.py::test_uses_perf_counter_not_wall_clock``.
    This test mirrors that guard onto the standalone distribution so
    neither side can silently revert Step 13 timing to ``time.time``.

    Strategy: patch ``time.perf_counter`` at the module level to a
    deterministic 1-per-call counter, patch ``time.time`` to a constant,
    run the standalone's ``run_latency_variance`` against a mock client,
    then assert:
      * perf_counter invoked >= 2 times per probe (t0 + elapsed)
      * time.time never invoked during the timing loop
      * latencies exactly equal to the fake clock deltas

    Under a wall-clock implementation these assertions fail loudly
    because the mock client returns instantaneously (elapsed ~ 0),
    whereas our fake perf_counter yields elapsed = 1.0 per probe.
    """
    import time as time_mod
    from unittest.mock import MagicMock

    perf_counter_calls = [0]
    time_time_calls = [0]
    counter = [0]

    def fake_perf_counter():
        perf_counter_calls[0] += 1
        counter[0] += 1
        return float(counter[0])

    def fake_time():
        time_time_calls[0] += 1
        return 1_700_000_000.0

    monkeypatch.setattr(time_mod, "perf_counter", fake_perf_counter)
    monkeypatch.setattr(time_mod, "time", fake_time)

    standalone = _load_standalone_audit()

    client = MagicMock()
    client.ensure_format = MagicMock()
    client.call = MagicMock(return_value={
        "text": "ok",
        "input_tokens": 1,
        "output_tokens": 1,
        "raw": {},
        "time": 0.0,
    })

    result = standalone.run_latency_variance(client, count=3, sleep=0)

    assert perf_counter_calls[0] >= 6, (
        f"Standalone audit.py invoked perf_counter "
        f"{perf_counter_calls[0]} times; expected >= 6 for 3 probes. "
        f"Step 13 may have reverted to time.time() in the standalone "
        f"distribution, which would silently re-introduce wall-clock "
        f"artifacts."
    )
    assert time_time_calls[0] == 0, (
        f"Standalone audit.py called time.time() {time_time_calls[0]} "
        f"times during latency-variance timing; must use monotonic "
        f"perf_counter only."
    )
    assert result["latencies"] == [1.0, 1.0, 1.0]


# ---------------------------------------------------------------------------
# Story-1 (S1-probe-core/types) probe types Section parity
# ---------------------------------------------------------------------------


def _extract_probe_types_section(path: Path) -> str:
    """Slice the ``# === probe types ===`` block from a file. Both
    ``audit.py`` (standalone) and any sibling reference that participates in
    the dual-distribution invariant MUST surround the probe types block with
    ``# === probe types ===`` / ``# === /probe types ===`` markers."""
    text = path.read_text(encoding="utf-8")
    start_marker = "# === probe types ===\n"
    end_marker = "# === /probe types ===\n"
    start = text.find(start_marker)
    end = text.find(end_marker, start)
    if start == -1:
        raise AssertionError(
            f"Could not find '# === probe types ===' opening marker in {path}"
        )
    if end == -1:
        raise AssertionError(
            f"Could not find '# === /probe types ===' closing marker in {path}"
        )
    return text[start + len(start_marker):end]


def test_probe_types_section_present_in_standalone():
    """Story-1 dual-distribution: standalone ``audit.py`` must inline the
    probe types dataclasses inside a ``# === probe types ===`` Section block.
    """
    section = _extract_probe_types_section(REPO_ROOT / "audit.py")
    # Sanity: every dataclass that downstream slices consume must appear.
    for needle in (
        "PROBE_STATUS",
        "PROBE_VERDICT",
        "class ProbeError",
        "class ReachabilityResult",
        "class AuthSniffResult",
        "class ModelsDiffResult",
        "class RateLimitResult",
        "class InfraHint",
        "class ProbeReport",
        'SCHEMA_VERSION = "1.0"',
    ):
        assert needle in section, (
            f"Standalone audit.py probe types Section block missing {needle!r}; "
            "dual-distribution invariant would diverge from "
            "api_relay_audit/probe/types.py."
        )


def test_probe_types_constants_parity():
    """Story-1 dual-distribution: PROBE_STATUS, PROBE_VERDICT, and the
    frozen schema_version literal must match character-for-character
    between ``api_relay_audit/probe/types.py`` and standalone audit.py.
    """
    from api_relay_audit.probe.types import (
        PROBE_STATUS as MODULAR_STATUS,
        PROBE_VERDICT as MODULAR_VERDICT,
        ProbeReport as ModularProbeReport,
    )

    standalone = _load_standalone_audit()

    assert standalone.PROBE_STATUS == MODULAR_STATUS, (
        "PROBE_STATUS drift between api_relay_audit/probe/types.py "
        "and standalone audit.py."
    )
    assert standalone.PROBE_VERDICT == MODULAR_VERDICT, (
        "PROBE_VERDICT drift between api_relay_audit/probe/types.py "
        "and standalone audit.py."
    )
    assert standalone.ProbeReport.SCHEMA_VERSION == ModularProbeReport.SCHEMA_VERSION, (
        "ProbeReport.SCHEMA_VERSION drift between distributions."
    )


def test_probe_dataclass_fields_parity():
    """Each Story-1 dataclass must declare an identical set of field names
    + defaults between the modular and standalone distributions."""
    from dataclasses import fields as dc_fields

    import api_relay_audit.probe.types as modular_types

    standalone = _load_standalone_audit()

    for cls_name in (
        "ProbeError",
        "ReachabilityResult",
        "AuthSniffResult",
        "ModelsDiffResult",
        "RateLimitResult",
        "InfraHint",
        "ProbeReport",
    ):
        modular_cls = getattr(modular_types, cls_name)
        standalone_cls = getattr(standalone, cls_name)
        modular_field_names = [f.name for f in dc_fields(modular_cls)]
        standalone_field_names = [f.name for f in dc_fields(standalone_cls)]
        assert modular_field_names == standalone_field_names, (
            f"Field-name order drift on {cls_name} between modular "
            f"api_relay_audit/probe/types.py and standalone audit.py: "
            f"modular={modular_field_names!r}, standalone={standalone_field_names!r}"
        )


# ---------------------------------------------------------------------------
# Story-2 (S1-probe-core/reachability) probe.reachability Section parity
# ---------------------------------------------------------------------------


def _extract_probe_reachability_section(path: Path) -> str:
    """Slice the ``# === probe.reachability ===`` block from a file."""
    text = path.read_text(encoding="utf-8")
    start_marker = "# === probe.reachability ===\n"
    end_marker = "# === /probe.reachability ===\n"
    start = text.find(start_marker)
    end = text.find(end_marker, start)
    if start == -1:
        raise AssertionError(
            f"Could not find '# === probe.reachability ===' opening marker in {path}"
        )
    if end == -1:
        raise AssertionError(
            f"Could not find '# === /probe.reachability ===' closing marker in {path}"
        )
    return text[start + len(start_marker):end]


def test_probe_reachability_section_present_in_standalone():
    """Story-2 dual-distribution: standalone ``audit.py`` must inline a
    ``probe_reachability`` implementation inside a ``# === probe.reachability ===``
    Section block (character-checked sentinels).
    """
    section = _extract_probe_reachability_section(REPO_ROOT / "audit.py")
    for needle in (
        "def probe_reachability(",
        '"reachability:via-curl"',
        '"reachability:dns-failed"',
        '"reachability:tcp-refused"',
        '"reachability:tls-failed"',
        '"reachability:http-4xx"',
        '"reachability:http-5xx"',
        '"reachability:timeout"',
        '"reachability:invalid-base-url"',
        '"dns_resolution_failed"',
        '"tcp_refused"',
        '"tls_handshake_failed"',
        '"http_5xx"',
        '"invalid_base_url"',
        '"timeout"',
    ):
        assert needle in section, (
            f"Standalone audit.py probe.reachability Section block missing {needle!r}; "
            "dual-distribution invariant would diverge from "
            "api_relay_audit/probe/reachability.py."
        )


def test_probe_reachability_branch_parity(monkeypatch):
    """Story-2 (TES-150) functional parity: the standalone and modular
    ``probe_reachability`` implementations MUST return identical
    (status, error.code or None, sorted-signals, fallback_to_curl)
    tuples on the six PRD §6.2 A4 mock branches plus the curl-fallback
    recovery branch.

    The transports differ (httpx vs curl-strict) so we monkeypatch each
    side's network seam to identical synthetic outcomes, then assert the
    classification layer is character-equivalent. We use ``monkeypatch``
    explicitly so attribute restoration happens at teardown — direct
    assignment would leak seams into subsequent test modules.
    """
    import socket as _socket
    import httpx as _httpx

    from api_relay_audit.probe import reachability as modular_p1

    standalone = _load_standalone_audit()

    def _summary(result):
        return (
            result.status,
            (result.error.code if result.error is not None else None),
            tuple(sorted(result.signals)),
            result.fallback_to_curl,
            result.http_status_root,
        )

    class _FakeResp:
        def __init__(self, code):
            self.status_code = code

    def _run_branch(name, dns_fn, modular_first, standalone_first, recovery_code):
        # Reset seams via monkeypatch so they get restored at teardown.
        monkeypatch.setattr(modular_p1, "_resolve_host", dns_fn)
        monkeypatch.setattr(standalone, "_probe_resolve_host", dns_fn)

        if name == "dns_fail":
            modular_result = modular_p1.probe_reachability(
                "https://no-such-host.invalid/v1", timeout_s=1
            )
            standalone_result = standalone.probe_reachability(
                "https://no-such-host.invalid/v1", timeout_s=1
            )
        elif name == "curl_fallback_recovery":
            def _modular_httpx(url, timeout):
                raise modular_first

            def _modular_curl(url, timeout):
                return _FakeResp(recovery_code)

            monkeypatch.setattr(modular_p1, "_httpx_get_root", _modular_httpx)
            monkeypatch.setattr(modular_p1, "_curl_get_root", _modular_curl)
            modular_result = modular_p1.probe_reachability(
                "https://relay.example/v1", timeout_s=1
            )

            def _standalone_curl(url, timeout, insecure):
                if not insecure:
                    raise standalone_first
                return _FakeResp(recovery_code)

            monkeypatch.setattr(standalone, "_probe_curl_get_root", _standalone_curl)
            standalone_result = standalone.probe_reachability(
                "https://relay.example/v1", timeout_s=1
            )
        else:
            def _modular_httpx(url, timeout):
                if isinstance(modular_first, Exception):
                    raise modular_first
                return modular_first

            monkeypatch.setattr(modular_p1, "_httpx_get_root", _modular_httpx)
            modular_result = modular_p1.probe_reachability(
                "https://relay.example/v1", timeout_s=1
            )

            def _standalone_curl(url, timeout, insecure):
                if isinstance(standalone_first, Exception):
                    raise standalone_first
                return standalone_first

            monkeypatch.setattr(standalone, "_probe_curl_get_root", _standalone_curl)
            standalone_result = standalone.probe_reachability(
                "https://relay.example/v1", timeout_s=1
            )

        assert _summary(modular_result) == _summary(standalone_result), (
            f"Branch parity drift on {name!r}: "
            f"modular={_summary(modular_result)} "
            f"standalone={_summary(standalone_result)}"
        )

    # 1) DNS failure
    def _dns_fail(host):
        raise _socket.gaierror(-2, "no such host")

    _run_branch("dns_fail", _dns_fail, None, None, None)

    # 2) TCP refused (no SSL hint in message)
    _run_branch(
        "tcp_refused",
        lambda h: True,
        _httpx.ConnectError("Connection refused"),
        RuntimeError("curl failed (rc=7): Connection refused"),
        None,
    )

    # 3) TLS handshake — both transports fail on retry too
    _run_branch(
        "tls_failed",
        lambda h: True,
        _httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
        RuntimeError("curl failed (rc=60): SSL certificate problem"),
        None,
    )

    # 4) HTTP 200
    _run_branch("http_200", lambda h: True, _FakeResp(200), _FakeResp(200), 200)

    # 5) HTTP 401 (4xx)
    _run_branch("http_401", lambda h: True, _FakeResp(401), _FakeResp(401), 401)

    # 6) HTTP 502 (5xx)
    _run_branch("http_502", lambda h: True, _FakeResp(502), _FakeResp(502), 502)

    # 7) curl-fallback recovery: httpx SSL fails, curl -sk returns 200
    _run_branch(
        "curl_fallback_recovery",
        lambda h: True,
        _httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]"),
        RuntimeError("curl failed (rc=60): SSL certificate problem"),
        200,
    )


def test_standalone_stream_model_helper_parity():
    """Regression: missing message_start.model must no longer pass as
    Claude-like on either distribution."""
    from api_relay_audit.stream_integrity import StreamSignals, _check_stream_model

    standalone = _load_standalone_audit()

    cases = [None, "claude-opus-4-6", "gpt-5"]
    for model in cases:
        modular_signals = StreamSignals()
        modular_signals.message_start_model = model

        standalone_signals = standalone.StreamSignals()
        standalone_signals.message_start_model = model

        assert _check_stream_model(modular_signals) == standalone._check_stream_model(
            standalone_signals
        ), f"Standalone stream-model helper drift for model={model!r}"
