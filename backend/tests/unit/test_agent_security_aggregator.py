"""
tests/unit/test_agent_security_aggregator.py — Patch 7f-3.

Covers agents/security_aggregator.py's run(): deterministic, no-LLM
de-duplication of a single module's own `findings` list (per-module,
never across modules -- see module docstring), keyed on word/char
similarity via utils.similarity.similarity(). Locks down:

  1. run() short-circuits on empty/missing security_scan_results,
     returning it unchanged (never writes a fabricated report).
  2. _dedupe_findings(): pairs above SIMILARITY_THRESHOLD merge;
     below it, both are kept distinct.
  3. _merge_pair(): higher severity always wins (never silently
     downgrades), longer description always wins.
  4. Malformed findings (not a dict, or missing "description") pass
     through untouched rather than being merged or dropped.
  5. run() overwrites KEYS["security_scan_results"] in place with the
     deduped shape, preserving non-dict module results as-is.
"""
import agents.security_aggregator as security_aggregator


# ---------------------------------------------------------------------------
# 1. run() short-circuit on empty input
# ---------------------------------------------------------------------------
class TestEmptyInput:
    def test_no_scan_results_returns_empty_dict_unchanged(self, fake_bus, monkeypatch):
        monkeypatch.setattr(security_aggregator, "read", lambda *a, **k: {})
        writes = []
        monkeypatch.setattr(security_aggregator, "write", lambda *a, **k: writes.append(a))
        result = security_aggregator.run()
        assert result == {}
        assert writes == []  # never writes a fabricated report when there's nothing to aggregate

    def test_missing_key_entirely_returns_default_empty_dict(self, fake_bus, monkeypatch):
        monkeypatch.setattr(security_aggregator, "read", lambda key, default=None: default)
        result = security_aggregator.run()
        assert result == {}


# ---------------------------------------------------------------------------
# 2. _dedupe_findings(): similarity threshold behavior
# ---------------------------------------------------------------------------
class TestDedupeFindings:
    def test_similar_descriptions_are_merged(self):
        findings = [
            {"severity": "critical", "description": "hardcoded API key found in config file at line 12"},
            {"severity": "moderate", "description": "hardcoded secret key exposed, credential exposure risk"},
        ]
        deduped, merge_count = security_aggregator._dedupe_findings(findings)
        assert merge_count == 1
        assert len(deduped) == 1

    def test_dissimilar_descriptions_are_kept_separate(self):
        findings = [
            {"severity": "critical", "description": "SQL injection vulnerability in user input handling"},
            {"severity": "minor", "description": "unclosed database connection may leak resources"},
        ]
        deduped, merge_count = security_aggregator._dedupe_findings(findings)
        assert merge_count == 0
        assert len(deduped) == 2

    def test_three_similar_findings_all_merge_into_one(self):
        findings = [
            {"severity": "minor", "description": "hardcoded API key found in config"},
            {"severity": "moderate", "description": "hardcoded secret key found in config"},
            {"severity": "critical", "description": "hardcoded credential key found in config"},
        ]
        deduped, merge_count = security_aggregator._dedupe_findings(findings)
        assert len(deduped) == 1
        assert merge_count == 2

    def test_merged_count_bookkeeping_field_is_stripped_from_output(self):
        findings = [
            {"severity": "minor", "description": "hardcoded API key found in config"},
            {"severity": "moderate", "description": "hardcoded secret key found in config"},
        ]
        deduped, _ = security_aggregator._dedupe_findings(findings)
        assert "_merged_count" not in deduped[0]

    def test_empty_findings_list_returns_empty(self):
        deduped, merge_count = security_aggregator._dedupe_findings([])
        assert deduped == []
        assert merge_count == 0

    def test_single_finding_is_unchanged(self):
        findings = [{"severity": "critical", "description": "SQL injection in login form"}]
        deduped, merge_count = security_aggregator._dedupe_findings(findings)
        assert deduped == findings
        assert merge_count == 0


# ---------------------------------------------------------------------------
# 3. _merge_pair(): severity and description precedence
# ---------------------------------------------------------------------------
class TestMergePair:
    def test_higher_severity_wins_when_kept_is_lower(self):
        kept = {"severity": "moderate", "description": "short desc"}
        incoming = {"severity": "critical", "description": "x"}
        merged = security_aggregator._merge_pair(kept, incoming)
        assert merged["severity"] == "critical"

    def test_critical_is_never_downgraded_by_a_moderate_duplicate(self):
        kept = {"severity": "critical", "description": "a much longer and more detailed description here"}
        incoming = {"severity": "moderate", "description": "short"}
        merged = security_aggregator._merge_pair(kept, incoming)
        assert merged["severity"] == "critical"

    def test_equal_severity_keeps_kept_severity(self):
        kept = {"severity": "moderate", "description": "short"}
        incoming = {"severity": "moderate", "description": "also short"}
        merged = security_aggregator._merge_pair(kept, incoming)
        assert merged["severity"] == "moderate"

    def test_longer_description_wins_regardless_of_which_side(self):
        kept = {"severity": "minor", "description": "short"}
        incoming = {"severity": "minor", "description": "a much longer, more detailed description"}
        merged = security_aggregator._merge_pair(kept, incoming)
        assert merged["description"] == "a much longer, more detailed description"

    def test_kept_longer_description_stays_when_kept_is_longer(self):
        kept = {"severity": "minor", "description": "a much longer, more detailed description"}
        incoming = {"severity": "minor", "description": "short"}
        merged = security_aggregator._merge_pair(kept, incoming)
        assert merged["description"] == "a much longer, more detailed description"

    def test_merged_count_increments_from_kept_or_starts_at_1(self):
        kept = {"severity": "minor", "description": "abc"}
        incoming = {"severity": "minor", "description": "xyz"}
        merged = security_aggregator._merge_pair(kept, incoming)
        assert merged["_merged_count"] == 2


# ---------------------------------------------------------------------------
# 4. Malformed findings pass through untouched
# ---------------------------------------------------------------------------
class TestMalformedFindingsPassThrough:
    def test_non_dict_finding_passes_through(self):
        findings = ["just a string finding", {"severity": "minor", "description": "real one"}]
        deduped, merge_count = security_aggregator._dedupe_findings(findings)
        assert "just a string finding" in deduped
        assert merge_count == 0

    def test_dict_missing_description_passes_through(self):
        findings = [{"severity": "critical"}, {"severity": "minor", "description": "real one"}]
        deduped, merge_count = security_aggregator._dedupe_findings(findings)
        assert {"severity": "critical"} in deduped
        assert merge_count == 0

    def test_malformed_entry_never_merges_into_a_real_finding(self):
        findings = [
            {"severity": "critical"},  # no description
            {"severity": "minor", "description": "hardcoded API key found"},
            {"severity": "minor", "description": "hardcoded secret key found"},
        ]
        deduped, merge_count = security_aggregator._dedupe_findings(findings)
        # The two real, similar findings merge; the malformed one stays separate.
        assert len(deduped) == 2
        assert merge_count == 1


# ---------------------------------------------------------------------------
# 5. run(): overwrite behavior and non-dict module results
# ---------------------------------------------------------------------------
class TestRunOverwrite:
    def test_overwrites_security_scan_results_key_in_place(self, fake_bus, monkeypatch):
        scan_results = {
            "auth_module": {"findings": [
                {"severity": "minor", "description": "hardcoded API key found in config"},
                {"severity": "moderate", "description": "hardcoded secret key found in config"},
            ]},
        }
        monkeypatch.setattr(security_aggregator, "read", lambda *a, **k: scan_results)
        writes = {}
        monkeypatch.setattr(security_aggregator, "write", lambda key, val: writes.__setitem__(key, val))

        result = security_aggregator.run()
        assert writes[security_aggregator.KEYS["security_scan_results"]] == result
        assert len(result["auth_module"]["findings"]) == 1

    def test_non_dict_module_result_passed_through_unchanged(self, fake_bus, monkeypatch):
        scan_results = {"broken_module": "scan failed entirely"}
        monkeypatch.setattr(security_aggregator, "read", lambda *a, **k: scan_results)
        monkeypatch.setattr(security_aggregator, "write", lambda *a, **k: None)

        result = security_aggregator.run()
        assert result["broken_module"] == "scan failed entirely"

    def test_module_result_with_error_field_is_preserved_alongside_deduped_findings(self, fake_bus, monkeypatch):
        scan_results = {
            "flaky_module": {
                "error": "sandbox timeout",
                "findings": [{"severity": "minor", "description": "a lone finding"}],
            },
        }
        monkeypatch.setattr(security_aggregator, "read", lambda *a, **k: scan_results)
        monkeypatch.setattr(security_aggregator, "write", lambda *a, **k: None)

        result = security_aggregator.run()
        assert result["flaky_module"]["error"] == "sandbox timeout"
        assert len(result["flaky_module"]["findings"]) == 1

    def test_multiple_modules_are_deduped_independently_never_across_modules(self, fake_bus, monkeypatch):
        scan_results = {
            "mod_a": {"findings": [{"severity": "minor", "description": "hardcoded API key found in config"}]},
            "mod_b": {"findings": [{"severity": "minor", "description": "hardcoded API key found in config"}]},
        }
        monkeypatch.setattr(security_aggregator, "read", lambda *a, **k: scan_results)
        monkeypatch.setattr(security_aggregator, "write", lambda *a, **k: None)

        result = security_aggregator.run()
        # Identical findings in DIFFERENT modules never merge with each other.
        assert len(result["mod_a"]["findings"]) == 1
        assert len(result["mod_b"]["findings"]) == 1
