"""
tests/unit/test_agent_pagespeed_agent.py — Patch 7f-5.

Covers agents/pagespeed_agent.py's run_audit(): a REAL_ACTION_ROLES tool
agent, zero LLM calls, one keyless HTTP GET against Google's PageSpeed
Insights API.

requests.get() is faked at the module level (pagespeed_agent.requests),
same posture test_agent_academic_search.py takes for its own
requests.get()-calling _search_* functions — no real network call under
test.
"""
from unittest.mock import MagicMock

import pytest

import agents.pagespeed_agent as pagespeed_agent


class _FakeResponse:
    def __init__(self, json_data=None, text="", status=200, raise_json_error=False):
        self._json_data = json_data
        self.text = text
        self.status_code = status
        self._raise_json_error = raise_json_error

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        if self._raise_json_error:
            raise ValueError("not json")
        return self._json_data


def _lighthouse_payload(perf=0.95, a11y=0.5, best_practices=None, seo=0.8,
                         extra_audits=None):
    categories = {}
    if perf is not None:
        categories["performance"] = {"score": perf}
    if a11y is not None:
        categories["accessibility"] = {"score": a11y}
    if best_practices is not None:
        categories["best-practices"] = {"score": best_practices}
    if seo is not None:
        categories["seo"] = {"score": seo}

    audits = {
        "first-contentful-paint": {"title": "First Contentful Paint", "score": 0.95},
        "uses-webp-images": {"title": "Uses WebP Images", "score": 0.4},
        "color-contrast": {"title": "Color Contrast", "score": 0.6},
        "no-score-audit": {"title": "Informational only", "score": None},
    }
    if extra_audits:
        audits.update(extra_audits)

    return {
        "lighthouseResult": {"categories": categories, "audits": audits},
        "analysisUTCTimestamp": "2026-08-25T00:00:00Z",
    }


class TestValidation:
    def test_blank_url_raises_value_error(self):
        with pytest.raises(ValueError):
            pagespeed_agent.run_audit("   ")

    def test_missing_url_raises_value_error(self):
        with pytest.raises(ValueError):
            pagespeed_agent.run_audit("")

    def test_invalid_strategy_raises_value_error(self, monkeypatch):
        monkeypatch.setattr(pagespeed_agent, "requests", MagicMock())
        with pytest.raises(ValueError):
            pagespeed_agent.run_audit("https://example.com", strategy="tablet")


class TestNetworkErrors:
    def test_timeout_raises_pagespeed_error_504(self, monkeypatch):
        fake_requests = MagicMock()
        fake_requests.exceptions = pagespeed_agent.requests.exceptions
        fake_requests.get.side_effect = pagespeed_agent.requests.exceptions.Timeout()
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        with pytest.raises(pagespeed_agent.PageSpeedError) as exc_info:
            pagespeed_agent.run_audit("https://example.com")
        assert exc_info.value.status_code == 504

    def test_connection_error_raises_pagespeed_error_502(self, monkeypatch):
        fake_requests = MagicMock()
        fake_requests.exceptions = pagespeed_agent.requests.exceptions
        fake_requests.get.side_effect = pagespeed_agent.requests.exceptions.ConnectionError("down")
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        with pytest.raises(pagespeed_agent.PageSpeedError) as exc_info:
            pagespeed_agent.run_audit("https://example.com")
        assert exc_info.value.status_code == 502


class TestApiErrorResponses:
    def test_error_json_detail_and_400_status_preserved(self, monkeypatch):
        resp = _FakeResponse(
            json_data={"error": {"message": "Invalid URL provided"}},
            status=400,
        )
        fake_requests = MagicMock()
        fake_requests.exceptions = pagespeed_agent.requests.exceptions
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        with pytest.raises(pagespeed_agent.PageSpeedError) as exc_info:
            pagespeed_agent.run_audit("https://bad-url")
        assert "Invalid URL provided" in str(exc_info.value)
        assert exc_info.value.status_code == 400

    def test_429_status_preserved(self, monkeypatch):
        resp = _FakeResponse(json_data={"error": {"message": "Quota exceeded"}}, status=429)
        fake_requests = MagicMock()
        fake_requests.exceptions = pagespeed_agent.requests.exceptions
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        with pytest.raises(pagespeed_agent.PageSpeedError) as exc_info:
            pagespeed_agent.run_audit("https://example.com")
        assert exc_info.value.status_code == 429

    def test_other_error_status_collapses_to_502(self, monkeypatch):
        resp = _FakeResponse(json_data={"error": {"message": "Server error"}}, status=500)
        fake_requests = MagicMock()
        fake_requests.exceptions = pagespeed_agent.requests.exceptions
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        with pytest.raises(pagespeed_agent.PageSpeedError) as exc_info:
            pagespeed_agent.run_audit("https://example.com")
        assert exc_info.value.status_code == 502

    def test_unparseable_error_body_falls_back_to_raw_text(self, monkeypatch):
        resp = _FakeResponse(text="<html>gateway error</html>", status=502, raise_json_error=True)
        fake_requests = MagicMock()
        fake_requests.exceptions = pagespeed_agent.requests.exceptions
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        with pytest.raises(pagespeed_agent.PageSpeedError) as exc_info:
            pagespeed_agent.run_audit("https://example.com")
        assert "gateway error" in str(exc_info.value)


class TestHappyPath:
    def test_scores_rounded_to_int_and_missing_category_is_none(self, monkeypatch):
        resp = _FakeResponse(json_data=_lighthouse_payload(perf=0.957, a11y=0.5, best_practices=None, seo=0.8))
        fake_requests = MagicMock()
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)
        monkeypatch.setattr(pagespeed_agent, "PAGESPEED_API_KEY", None)

        result = pagespeed_agent.run_audit("https://example.com", strategy="desktop")

        assert result["scores"] == {"performance": 96, "accessibility": 50, "best_practices": None, "seo": 80}
        assert result["url"] == "https://example.com"
        assert result["strategy"] == "desktop"
        assert result["fetched_at"] == "2026-08-25T00:00:00Z"

    def test_issues_filtered_below_threshold_and_sorted_worst_first(self, monkeypatch):
        resp = _FakeResponse(json_data=_lighthouse_payload())
        fake_requests = MagicMock()
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        result = pagespeed_agent.run_audit("https://example.com")

        # first-contentful-paint (score 95) is above the 0.9 threshold and
        # excluded; the informational null-score audit never qualifies as
        # an issue either.
        issue_ids = [i["id"] for i in result["issues"]]
        assert issue_ids == ["uses-webp-images", "color-contrast"]
        assert [i["score"] for i in result["issues"]] == [40, 60]

    def test_default_strategy_is_mobile_and_requests_all_four_categories(self, monkeypatch):
        resp = _FakeResponse(json_data=_lighthouse_payload())
        fake_requests = MagicMock()
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)

        result = pagespeed_agent.run_audit("https://example.com")

        assert result["strategy"] == "mobile"
        _, call_kwargs = fake_requests.get.call_args
        assert call_kwargs["params"]["strategy"] == "mobile"
        assert call_kwargs["params"]["category"] == pagespeed_agent.CATEGORIES

    def test_api_key_included_when_set(self, monkeypatch):
        resp = _FakeResponse(json_data=_lighthouse_payload())
        fake_requests = MagicMock()
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)
        monkeypatch.setattr(pagespeed_agent, "PAGESPEED_API_KEY", "secret-key")

        pagespeed_agent.run_audit("https://example.com")

        _, call_kwargs = fake_requests.get.call_args
        assert call_kwargs["params"]["key"] == "secret-key"

    def test_api_key_omitted_when_unset(self, monkeypatch):
        resp = _FakeResponse(json_data=_lighthouse_payload())
        fake_requests = MagicMock()
        fake_requests.get.return_value = resp
        monkeypatch.setattr(pagespeed_agent, "requests", fake_requests)
        monkeypatch.setattr(pagespeed_agent, "PAGESPEED_API_KEY", None)

        pagespeed_agent.run_audit("https://example.com")

        _, call_kwargs = fake_requests.get.call_args
        assert "key" not in call_kwargs["params"]
