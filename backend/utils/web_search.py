"""
utils/web_search.py — shared web-search core, extracted out of
agents/part_price_finder.py's original _search_tavily()/_search_brave()
pair (task 13a).

PROVIDER NOTE: Brave's free-tier Search API has been discontinued -- there
is no free key to request any more, so the Brave branch part_price_finder
used to fall back to (_search_brave(), BRAVE_URL, the BRAVE_API_KEY env
var) is dropped entirely, not ported. Exa (EXA_API_KEY) was added as the
fallback provider instead -- see _search_exa() below.

search(query, domains=None, max_results=3) -> list[{"url", "snippet", "title"}]

Provider order: Tavily first, Exa only if Tavily returns nothing (missing
key, request failure, or a real "no results" response all look the same
from search()'s point of view -- an empty list -- so all three fall
through to Exa the same way). This is the same "walk an ordered list,
only fall through on failure" shape as utils/llm_client.py's per-agent
CHAIN, just not exposed as a caller-supplied parameter: every current
caller wants "best available free search," not a specific provider.

Domain scoping: pass the FULL list in `domains` for a single call -- both
providers' domain-filter params take a list natively (Tavily:
include_domains, Exa: includeDomains), so a fixed research scope (e.g.
Reddit + a couple of forums) should go in as one call, one list.
part_price_finder.py is the one exception: it checks a fixed per-vendor
allowlist one BD_VENDOR_DOMAINS entry at a time (so it can report which
specific vendor a listing came from), and does that by calling
search(query, domains=[single_domain]) in its own loop -- that looping is
part_price_finder's own choice, not this module's.
"""
import os

import requests

from utils.llm_client import log_usage

from datetime import UTC, datetime

from memory.bus import read as bus_read
from memory.bus import write as bus_write

TAVILY_URL = "https://api.tavily.com/search"
EXA_URL = "https://api.exa.ai/search"
REQUEST_TIMEOUT = 12


def search(query: str, domains: list[str] | None = None, max_results: int = 3,
           agent_name: str = "web_search") -> list[dict]:
    """Returns [{"url", "snippet", "title"}, ...]. Empty list only if BOTH
    providers come back empty (missing key, request failure, or a
    genuine no-results response) -- every current caller already treats
    "no results" as a normal, non-fatal outcome (see
    part_price_finder.py's `if not snippets` branch), so this never
    raises.

    `agent_name` is passed straight through to log_usage() so the usage
    dashboard attributes the call to whichever agent made it, rather
    than lumping every caller's search usage under one label.
    """
    results = _search_tavily(query, domains, max_results, agent_name)
    if results:
        return results
    return _search_exa(query, domains, max_results, agent_name)


# Patch T1: Tavily's plan quota (HTTP 432, "This request exceeds your
# plan's set usage limit") is monthly, not a short window -- retrying
# the SAME key later today never helps. Rotate through every
# TAVILY_API_KEY* env var configured (same naming convention this
# codebase already uses for GROQ_API_KEY_6.._14 / GEMINI_API_KEY_2.._18),
# and remember which keys are capped for a while so we don't waste a
# request re-discovering the same dead-for-the-month key every call.
_TAVILY_COOLDOWN_SECONDS = 6 * 3600  # cheap to re-check a few times/day if a key resets


def _tavily_key_env_names() -> list[str]:
    names = ["TAVILY_API_KEY"]
    i = 2
    while os.environ.get(f"TAVILY_API_KEY_{i}"):
        names.append(f"TAVILY_API_KEY_{i}")
        i += 1
    return names


def _tavily_key_cooling_down(key_env: str) -> bool:
    until = bus_read(f"cooldown_until:tavily:{key_env}", default=0)
    return bool(until) and datetime.now(UTC).timestamp() < until


def _set_tavily_cooldown(key_env: str) -> None:
    try:
        bus_write(f"cooldown_until:tavily:{key_env}",
                  datetime.now(UTC).timestamp() + _TAVILY_COOLDOWN_SECONDS)
    except Exception as write_exc:
        print(f"  [web_search] tavily cooldown write failed (non-fatal): {write_exc}")


def _search_tavily(query: str, domains: list[str] | None, max_results: int,
                    agent_name: str) -> list[dict]:
    for key_env in _tavily_key_env_names():
        if _tavily_key_cooling_down(key_env):
            continue
        key = os.environ.get(key_env)
        if not key:
            continue
        try:
            payload = {
                "api_key": key,
                "query": query,
                "max_results": max_results,
                "include_raw_content": False,
            }
            if domains:
                payload["include_domains"] = domains
            resp = requests.post(TAVILY_URL, json=payload, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 432:
                print(f"  [web_search] Tavily key {key_env} hit its plan "
                      f"usage limit (432) -- cooling it down for "
                      f"{_TAVILY_COOLDOWN_SECONDS // 3600}h and trying the "
                      f"next configured Tavily key, if any.")
                _set_tavily_cooldown(key_env)
                continue
            resp.raise_for_status()
            log_usage("tavily", key_env, tokens=None, agent_name=agent_name)
            return [{"url": r["url"], "snippet": r.get("content", ""), "title": r.get("title", "")}
                    for r in resp.json().get("results", [])]
        except Exception as exc:
            print(f"  [web_search] Tavily ({key_env}) failed: {exc}")
            continue
    return []


def _search_exa(query: str, domains: list[str] | None, max_results: int,
                 agent_name: str) -> list[dict]:
    """Raw REST call to Exa's /search endpoint (not the exa_py SDK --
    every other provider in this codebase is a plain requests.post(), and
    pulling in a provider SDK for one function here would be the odd one
    out). Per Exa's own setup guide's "raw retrieval for your own agent"
    pattern: contents.highlights (not top-level text/summary -- those
    only apply on /contents, not /search), type "auto" for the balanced
    default, includeDomains (camelCase; the guide's snake_case examples
    are exa_py-SDK-only, this is the raw JSON body).

    Auth header confirmed against Exa's canonical reference
    (docs.exa.ai/reference/search-api-guide-for-coding-agents, fetched
    directly rather than trusted from the uploaded setup doc, which
    didn't spell out the raw-HTTP header at all): `Authorization: Bearer
    $EXA_API_KEY`, NOT `x-api-key` -- that was this function's first,
    wrong guess and is why the first live call 401'd.
    """
    key = os.environ.get("EXA_API_KEY")
    if not key:
        return []
    try:
        payload = {
            "query": query,
            "type": "auto",
            "numResults": max_results,
            "contents": {"highlights": True},
        }
        if domains:
            payload["includeDomains"] = domains
        resp = requests.post(
            EXA_URL, json=payload, timeout=REQUEST_TIMEOUT,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        resp.raise_for_status()
        log_usage("exa", "EXA_API_KEY", tokens=None, agent_name=agent_name)
        out = []
        for r in resp.json().get("results", []):
            highlights = r.get("highlights") or []
            out.append({"url": r["url"], "snippet": " ".join(highlights), "title": r.get("title", "")})
        return out
    except Exception as exc:
        print(f"  [web_search] Exa failed: {exc}")
        return []
