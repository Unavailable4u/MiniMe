"""
agents/pagespeed_agent.py — Google PageSpeed Insights connector.

REAL_ACTION_ROLES tool agent, same shape as agents/calendar_agent.py:
zero LLM calls, one HTTP request to one external API, structured data in
and structured data out. Unlike calendar_agent.py, this call is NOT made
on behalf of a specific user — PageSpeed Insights is a free, keyless
public endpoint (an optional API key just raises the rate limit), so
there's no eo.integrations token to resolve and no
IntegrationNotConnectedError case. Closer in shape to how
calendar_agent.py's own docstring describes agents/academic_search.py.
"""
import os
import requests

PAGESPEED_API_BASE = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"
REQUEST_TIMEOUT = 30  # Lighthouse runs server-side on Google's end; this is slow by design, not a bug
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY")  # optional — works keyless at a lower quota

VALID_STRATEGIES = {"mobile", "desktop"}
CATEGORIES = ["performance", "accessibility", "best-practices", "seo"]

# Audits below this score are surfaced as "issues" in the result — same
# threshold Lighthouse's own CLI/report UI uses to color a row red/amber.
ISSUE_SCORE_THRESHOLD = 0.9


class PageSpeedError(Exception):
    """Raised when Google's API itself rejects the request (bad/unreachable
    URL, invalid key, quota exceeded) — surfaced as the API's own error
    message rather than swallowed, since 'audit failed' with no reason is
    useless to someone trying to fix their site."""
    def __init__(self, message: str, status_code: int = 502):
        self.status_code = status_code
        super().__init__(message)


def run_audit(url: str, strategy: str = "mobile") -> dict:
    """Result shape:
    {
        "url", "strategy",
        "scores": {"performance", "accessibility", "best_practices", "seo"},  # 0-100 ints, None if a category didn't run
        "issues": [{"id", "title", "score"}],  # audits scoring below ISSUE_SCORE_THRESHOLD, sorted worst-first
        "fetched_at": "<UTC ISO8601>",
    }
    """
    if not url or not url.strip():
        raise ValueError("url is required")
    if strategy not in VALID_STRATEGIES:
        raise ValueError(f"strategy must be one of {sorted(VALID_STRATEGIES)}, got {strategy!r}")

    params = {"url": url, "strategy": strategy}
    # requests supports repeated params via a list value under the same key
    params["category"] = CATEGORIES
    if PAGESPEED_API_KEY:
        params["key"] = PAGESPEED_API_KEY

    try:
        resp = requests.get(PAGESPEED_API_BASE, params=params, timeout=REQUEST_TIMEOUT)
    except requests.exceptions.Timeout:
        raise PageSpeedError("PageSpeed Insights took too long to respond (Lighthouse runs can be slow on large pages).", status_code=504)
    except requests.exceptions.RequestException as e:
        raise PageSpeedError(f"Couldn't reach PageSpeed Insights: {e}", status_code=502)

    if not resp.ok:
        # Google's error shape: {"error": {"code", "message", ...}}
        try:
            detail = resp.json().get("error", {}).get("message", resp.text)
        except ValueError:
            detail = resp.text
        raise PageSpeedError(detail, status_code=resp.status_code if resp.status_code in (400, 429) else 502)

    data = resp.json()
    lhr = data.get("lighthouseResult", {})
    categories = lhr.get("categories", {})

    def _score(cat_key):
        cat = categories.get(cat_key)
        if not cat or cat.get("score") is None:
            return None
        return round(cat["score"] * 100)

    scores = {
        "performance": _score("performance"),
        "accessibility": _score("accessibility"),
        "best_practices": _score("best-practices"),
        "seo": _score("seo"),
    }

    audits = lhr.get("audits", {})
    issues = [
        {"id": audit_id, "title": audit.get("title", audit_id), "score": round(audit["score"] * 100)}
        for audit_id, audit in audits.items()
        if audit.get("score") is not None and audit["score"] < ISSUE_SCORE_THRESHOLD
    ]
    issues.sort(key=lambda i: i["score"])

    return {
        "url": url,
        "strategy": strategy,
        "scores": scores,
        "issues": issues,
        "fetched_at": data.get("analysisUTCTimestamp"),
    }