"""Story-4 (S1-probe-core/P3 models-diff) TDD tests.

PRD §6.2 A6: pytest tests/test_probe_models_diff.py -v MUST pass with
coverage on three diff classes — extra_in_relay / missing_in_relay /
suspicious_aliases — plus catalog_version threading and the
``--models-ref-url`` external override.

Mocking style: ``client.get_models()`` returns a list of OpenAI-shaped
``{"id": "..."}`` dicts (matching the real ``api_relay_audit.client``
return contract). We do NOT exercise httpx/curl in this slice — that is
S1 Story-2's reachability concern. P3 is purely diff logic.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from api_relay_audit.probe import models_diff as md
from api_relay_audit.probe.types import ModelsDiffResult


REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "models_diff"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_case(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _mock_client_from_case(case: dict) -> MagicMock:
    """Build a MagicMock APIClient whose ``get_models()`` returns the
    ``data`` array straight out of the fixture mock_http_response."""
    body = case["mock_http_response"]["body"]
    client = MagicMock()
    client.get_models.return_value = list(body["data"])
    return client


# ---------------------------------------------------------------------------
# Module API surface
# ---------------------------------------------------------------------------


class TestPublicSurface:
    def test_fetch_models_diff_is_callable(self):
        assert callable(md.fetch_models_diff)

    def test_returns_models_diff_result(self):
        client = MagicMock()
        client.get_models.return_value = []
        result = md.fetch_models_diff(client, vendor_hint="openai")
        assert isinstance(result, ModelsDiffResult)


# ---------------------------------------------------------------------------
# A6.1 — normal relay (no diff)
# ---------------------------------------------------------------------------


class TestNormalRelay:
    def setup_method(self):
        self.case = _load_case("case_normal_relay_listing")
        self.client = _mock_client_from_case(self.case)
        self.result = md.fetch_models_diff(self.client, vendor_hint="openai")

    def test_status_is_ok(self):
        assert self.result.status == "ok"

    def test_no_error(self):
        assert self.result.error is None

    def test_declared_count_matches_mock(self):
        expected = len(self.case["mock_http_response"]["body"]["data"])
        assert self.result.declared_count == expected

    def test_declared_lists_ids(self):
        expected_ids = [m["id"] for m in self.case["mock_http_response"]["body"]["data"]]
        assert self.result.declared == expected_ids

    def test_no_extras(self):
        assert self.result.extra_in_relay == []

    def test_no_missing_top_priority(self):
        assert self.result.missing_in_relay == []

    def test_no_suspicious_aliases(self):
        assert self.result.suspicious_aliases == []

    def test_catalog_version_written(self):
        assert self.result.catalog_version == "2026-05-25"

    def test_official_reference_populated(self):
        assert "gpt-5.5" in self.result.official_reference

    def test_claimed_model_match_pass(self):
        assert self.result.claimed_model_match == "pass"


# ---------------------------------------------------------------------------
# A6.2 — inconsistent relay (extra + missing + suspicious all fire)
# ---------------------------------------------------------------------------


class TestInconsistentRelay:
    def setup_method(self):
        self.case = _load_case("case_inconsistent_relay_listing")
        self.client = _mock_client_from_case(self.case)
        # vendor_hint="auto" → merge all three vendor catalogs
        self.result = md.fetch_models_diff(self.client, vendor_hint="auto")
        self.expected = self.case["expected_diff"]

    def test_status_ok_even_when_diff_fires(self):
        # status is the per-probe transport status; the verdict-like
        # claimed_model_match captures whether the diff was clean.
        assert self.result.status == "ok"

    def test_extra_in_relay_matches_expected(self):
        assert sorted(self.result.extra_in_relay) == sorted(self.expected["extra_in_relay"])

    def test_missing_in_relay_contains_all_top_priority_per_vendor(self):
        expected_missing = (
            self.expected["missing_in_relay_top_priority_openai"]
            + self.expected["missing_in_relay_top_priority_anthropic"]
            + self.expected["missing_in_relay_top_priority_gemini"]
        )
        # All expected flagships should be reported as missing.
        for m in expected_missing:
            assert m in self.result.missing_in_relay, (
                f"top-priority {m!r} expected in missing_in_relay but absent"
            )

    def test_suspicious_aliases_match_expected(self):
        assert sorted(self.result.suspicious_aliases) == sorted(self.expected["suspicious_aliases"])

    def test_suspicious_is_subset_of_extra(self):
        # PRD §3.1 P3: suspicious_aliases ⊂ extra_in_relay
        for alias in self.result.suspicious_aliases:
            assert alias in self.result.extra_in_relay

    def test_internal_router_is_extra_but_not_suspicious(self):
        # Fixture notes: internal-router-001 / auto are relay-control
        # plumbing, NOT model-name laundering.
        assert "internal-router-001" in self.result.extra_in_relay
        assert "internal-router-001" not in self.result.suspicious_aliases
        assert "auto" in self.result.extra_in_relay
        assert "auto" not in self.result.suspicious_aliases

    def test_claimed_model_match_fail(self):
        # ≥1 suspicious + ≥1 missing top-priority + ≥1 extra
        # → claimed_model_match must downgrade to "fail".
        assert self.result.claimed_model_match == "fail"

    def test_catalog_version_written_in_auto_mode(self):
        # In auto mode catalog_version still ships, derived from the
        # primary catalog (per-vendor ones are uniform 2026-05-25).
        assert self.result.catalog_version == "2026-05-25"


# ---------------------------------------------------------------------------
# vendor_hint scope
# ---------------------------------------------------------------------------


class TestVendorHintScope:
    """When the relay declares an openai-only catalog and we ask
    vendor_hint='openai', anthropic/gemini flagships MUST NOT show up in
    missing_in_relay — that would be a false positive."""

    def test_openai_hint_does_not_flag_anthropic_missing(self):
        # Fixture case_normal advertises an openai-only relay.
        case = _load_case("case_normal_relay_listing")
        client = _mock_client_from_case(case)
        result = md.fetch_models_diff(client, vendor_hint="openai")
        assert "claude-opus-4-7" not in result.missing_in_relay
        assert "gemini-2.5-pro" not in result.missing_in_relay

    def test_anthropic_hint_only_compares_against_anthropic_catalog(self):
        # Build a relay listing that advertises ONLY anthropic models;
        # vendor_hint='anthropic' should flag missing claude flagships.
        client = MagicMock()
        client.get_models.return_value = [
            {"id": "claude-3-5-sonnet-20241022"},
            {"id": "claude-3-haiku"},
        ]
        result = md.fetch_models_diff(client, vendor_hint="anthropic")
        assert "claude-opus-4-7" in result.missing_in_relay
        assert "claude-sonnet-4-6" in result.missing_in_relay
        # but not openai/gemini flagships
        assert "gpt-5.5" not in result.missing_in_relay
        assert "gemini-2.5-pro" not in result.missing_in_relay


# ---------------------------------------------------------------------------
# suspicious_alias heuristic (PRD §3.1 P3 + fixture F3 seeds)
# ---------------------------------------------------------------------------


class TestSuspiciousAliasHeuristic:
    """At least one suspicious_alias positive example is mandated by DoD.
    These tests assert each diff_class in the F3 fixture taxonomy."""

    @pytest.mark.parametrize(
        "model_id",
        ["gpt-5-pro", "gpt-5-ultra", "claude-3-mini", "claude-4-flash"],
    )
    def test_tier_confusion_cross_vendor_flagged(self, model_id):
        assert md.is_suspicious_alias(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        ["gpt-4o-turbo-max", "gemini-pro-max"],
    )
    def test_stacked_tier_suffix_flagged(self, model_id):
        assert md.is_suspicious_alias(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        ["claude-opus-5", "gemini-3.0-ultra"],
    )
    def test_version_forward_fake_flagged(self, model_id):
        assert md.is_suspicious_alias(model_id) is True

    @pytest.mark.parametrize(
        "model_id",
        [
            "gpt-4o",  # legit OpenAI flagship
            "claude-3-5-sonnet-20241022",  # legit version-pinned
            "gemini-2.5-flash",  # legit Gemini fast tier
            "internal-router-001",  # relay plumbing — extra but NOT suspicious
            "auto",  # relay plumbing
        ],
    )
    def test_negatives_not_flagged(self, model_id):
        assert md.is_suspicious_alias(model_id) is False

    def test_gpt_4_turbo_dated_variant_positive_example(self):
        """DoD: ``at least 1 suspicious_alias positive example``.

        Story description names 'gpt-4-turbo-2024' as a canonical example
        of a model-name laundering pattern (stacks tier + year suffix in
        a way OpenAI does not). 'gpt-4-turbo-2024-04-09' IS legit (in the
        catalog) but the bare 'gpt-4-turbo-2024' without a real release
        suffix is the laundering form.
        """
        assert md.is_suspicious_alias("gpt-4-turbo-2024") is True


# ---------------------------------------------------------------------------
# transport / error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_empty_response_returns_error_status(self):
        client = MagicMock()
        client.get_models.return_value = []
        result = md.fetch_models_diff(client, vendor_hint="openai")
        assert result.status == "error"
        assert result.error is not None
        assert result.error.code  # non-empty

    def test_error_carries_machine_readable_code(self):
        client = MagicMock()
        client.get_models.return_value = []
        result = md.fetch_models_diff(client, vendor_hint="openai")
        # PRD §4.2: error code must be short + machine-readable
        assert result.error.code == "models_list_unavailable"

    def test_malformed_response_does_not_raise(self):
        """get_models() returning non-list-of-dicts must downgrade to
        status=error rather than blow up the caller."""
        client = MagicMock()
        client.get_models.return_value = "garbage"
        result = md.fetch_models_diff(client, vendor_hint="openai")
        assert result.status == "error"

    def test_items_without_id_key_skipped_cleanly(self):
        client = MagicMock()
        client.get_models.return_value = [
            {"id": "gpt-5.5"},
            {"name": "no-id-here"},  # malformed entry, skip
            {"id": "gpt-4o"},
        ]
        result = md.fetch_models_diff(client, vendor_hint="openai")
        assert result.declared == ["gpt-5.5", "gpt-4o"]
        assert result.status == "ok"


# ---------------------------------------------------------------------------
# --models-ref-url external override (PRD §10 + design §7 Story-4)
# ---------------------------------------------------------------------------


class TestModelsRefUrlOverride:
    def test_external_url_replaces_default_catalog(self, tmp_path):
        """A user passing --models-ref-url <path-or-url> swaps the
        default catalog with their own. We exercise the file path
        form here (simplest mock); URL fetching is its own concern
        and lives behind the same code path.
        """
        external = tmp_path / "custom_catalog.json"
        external.write_text(
            json.dumps(
                {
                    "catalog_version": "2099-12-31",
                    "vendor": "openai",
                    "top_priority": ["future-model-x"],
                    "models": ["future-model-x", "future-model-y"],
                }
            ),
            encoding="utf-8",
        )

        client = MagicMock()
        client.get_models.return_value = [{"id": "future-model-y"}]

        result = md.fetch_models_diff(
            client,
            vendor_hint="openai",
            models_ref_url=str(external),
        )

        # custom catalog drives the diff: future-model-x missing,
        # future-model-y declared, no extras.
        assert result.catalog_version == "2099-12-31"
        assert "future-model-x" in result.missing_in_relay
        assert result.extra_in_relay == []
        assert result.official_reference == ["future-model-x", "future-model-y"]


# ---------------------------------------------------------------------------
# catalog file integrity
# ---------------------------------------------------------------------------


class TestCatalogFileIntegrity:
    """The references/official_models_*.json files migrated from PM
    fixtures MUST stay structurally sound. These tests guard against
    accidental field drops during refactors."""

    REFERENCES_DIR = REPO_ROOT / "api_relay_audit" / "probe" / "references"

    @pytest.mark.parametrize("vendor", ["openai", "anthropic", "gemini"])
    def test_catalog_has_required_fields(self, vendor):
        catalog_path = self.REFERENCES_DIR / f"official_models_{vendor}.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        for required in ("catalog_version", "vendor", "models", "top_priority"):
            assert required in catalog, (
                f"references/official_models_{vendor}.json missing {required!r}"
            )

    @pytest.mark.parametrize("vendor", ["openai", "anthropic", "gemini"])
    def test_top_priority_is_subset_of_models(self, vendor):
        catalog_path = self.REFERENCES_DIR / f"official_models_{vendor}.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        models = set(catalog["models"])
        for tp in catalog["top_priority"]:
            assert tp in models, (
                f"top_priority {tp!r} not in models[] for {vendor}"
            )

    def test_combined_catalog_meets_prd_f2_floor(self):
        """PRD F2 floor: ≥80 model IDs across the three vendors."""
        total = 0
        for vendor in ("openai", "anthropic", "gemini"):
            catalog = json.loads(
                (self.REFERENCES_DIR / f"official_models_{vendor}.json").read_text(
                    encoding="utf-8"
                )
            )
            total += len(catalog["models"])
        assert total >= 80, f"PRD F2 floor violated: only {total} model IDs"


# ---------------------------------------------------------------------------
# vendor_breakdown + signals
# ---------------------------------------------------------------------------


class TestVendorBreakdownAndSignals:
    def test_vendor_breakdown_counts_per_vendor(self):
        client = MagicMock()
        client.get_models.return_value = [
            {"id": "gpt-5.5"},
            {"id": "gpt-4o"},
            {"id": "claude-3-haiku"},
            {"id": "gemini-2.5-pro"},
            {"id": "internal-router-001"},  # unknown vendor bucket
        ]
        result = md.fetch_models_diff(client, vendor_hint="auto")
        assert result.vendor_breakdown.get("openai") == 2
        assert result.vendor_breakdown.get("anthropic") == 1
        assert result.vendor_breakdown.get("gemini") == 1

    def test_signals_emitted_for_extra_and_suspicious(self):
        case = _load_case("case_inconsistent_relay_listing")
        client = _mock_client_from_case(case)
        result = md.fetch_models_diff(client, vendor_hint="auto")
        # signals should at least flag suspicious_alias and extra count
        joined = " ".join(result.signals).lower()
        assert "suspicious" in joined
        assert "extra" in joined


# ---------------------------------------------------------------------------
# key not leaked (PRD §6.2 A8 surface assertion within P3)
# ---------------------------------------------------------------------------


class TestNoKeyLeakInResult:
    def test_result_serialization_contains_no_sk_prefix(self):
        """ModelsDiffResult should never echo the raw key. Mock a relay
        that happens to advertise a key-like ID; the field is still
        ``declared``, but the key in the auth header MUST NOT appear."""
        client = MagicMock()
        # APIClient real key is set on the mock attribute, not in the
        # response. We assert the result NEVER touches client.api_key.
        client.api_key = "sk-test-DO-NOT-LEAK-abcdef1234567890"
        client.get_models.return_value = [{"id": "gpt-5.5"}]
        result = md.fetch_models_diff(client, vendor_hint="openai")

        serialized = json.dumps(
            {
                "declared": result.declared,
                "official_reference": result.official_reference,
                "extra_in_relay": result.extra_in_relay,
                "missing_in_relay": result.missing_in_relay,
                "suspicious_aliases": result.suspicious_aliases,
                "signals": result.signals,
            }
        )
        assert "sk-test" not in serialized
        assert client.api_key not in serialized


# ---------------------------------------------------------------------------
# Branch coverage for defensive paths
# ---------------------------------------------------------------------------


class TestDefensiveBranches:
    @pytest.mark.parametrize("bad", [None, 42, b"bytes", ""])
    def test_is_suspicious_alias_rejects_non_string(self, bad):
        assert md.is_suspicious_alias(bad) is False

    @pytest.mark.parametrize("bad", [None, 99, b"x"])
    def test_classify_vendor_rejects_non_string(self, bad):
        assert md._classify_vendor(bad) is None

    def test_catalog_load_failure_returns_error_status(self, tmp_path):
        """A missing/malformed --models-ref-url file MUST NOT raise.

        Story-4 only knows the user-supplied path; if the file vanishes
        between probe.run() prep and P3 execution, P3 must downgrade to
        status=error rather than blow up the aggregator.
        """
        client = MagicMock()
        client.get_models.return_value = [{"id": "gpt-5.5"}]
        result = md.fetch_models_diff(
            client,
            vendor_hint="openai",
            models_ref_url=str(tmp_path / "nope.json"),
        )
        assert result.status == "error"
        assert result.error is not None
        assert result.error.code == "catalog_load_failed"

    def test_models_ref_url_https_fetched_via_urlopen(self, monkeypatch):
        """When --models-ref-url is an http(s) URL, the implementation
        MUST go through stdlib urlopen (no extra dependency). We mock
        urlopen so the test never touches the network."""
        from io import BytesIO

        custom = {
            "catalog_version": "2030-01-01",
            "vendor": "openai",
            "top_priority": ["gpt-x"],
            "models": ["gpt-x"],
        }

        class FakeResponse:
            def __init__(self, payload: bytes):
                self._buf = BytesIO(payload)

            def read(self):
                return self._buf.read()

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

        captured = {}

        def fake_urlopen(url, timeout=5):
            captured["url"] = url
            captured["timeout"] = timeout
            return FakeResponse(json.dumps(custom).encode("utf-8"))

        monkeypatch.setattr("api_relay_audit.probe.models_diff.urlopen", fake_urlopen)

        client = MagicMock()
        client.get_models.return_value = [{"id": "gpt-x"}]
        result = md.fetch_models_diff(
            client,
            vendor_hint="openai",
            models_ref_url="https://example.invalid/catalog.json",
        )
        assert captured["url"] == "https://example.invalid/catalog.json"
        assert captured["timeout"] == 5
        assert result.catalog_version == "2030-01-01"
        assert result.extra_in_relay == []
