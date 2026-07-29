# MiniMe — Gemini + Mistral + Hugging Face Rollout: Standalone Final Guide

This replaces the three earlier revision docs. It doesn't reference them — everything
here was re-derived by reading your actual repo (`eo/registry.py`, `eo/panel.py`,
`eo/worker_pool.py`, `eo/inspector.py`, `utils/llm_client.py`, `utils/embedding.py`,
`agents/generic_worker.py`, and the real-action agent modules), not by trusting what
the earlier drafts assumed. That mattered: **a chunk of the earlier plan would have
added new keys as inert metadata that no code path ever reads.** §1 explains why, so
you don't repeat that mistake as you go.

Your `.env` already has the placeholder lines for this rollout: `GEMINI_API_KEY_1..13`,
`MISTRAL_API_KEY_2..7`, `HUGGINGFACE_API_KEY_2..7` (the `env(example).txt` in your repo
root). Fill Gemini keys in from https://aistudio.google.com/apikey, Mistral from
https://console.mistral.ai/api-keys, and Hugging Face from
https://huggingface.co/settings/tokens (Fine-grained token, **Inference** preset — no
repo/write/account scopes needed for any of these).

---

## 1. The one thing every earlier draft got wrong: not every registry tag does anything

`eo/registry.py`'s `AGENT_CAPABILITIES` dict is only consulted by two things:

- `eo/panel.py`'s `_best_match()` — used by `staff_task()` for any role that resolves
  to the generic string `"generic_worker"`.
- `eo/worker_pool.py`'s `_select_workers()` — used by parallel pools that call it
  directly with their own `role_tag` (code_writers, reviewer, security_scanner,
  extraction_table_builder, note_table_builder, content_adapter_pool).

Which of those two a role uses (or neither) depends on `eo/registry.py`'s
`REAL_ACTION_ROLES` dict and `resolve_role()`:

```python
def resolve_role(role_name: str) -> str:
    return REAL_ACTION_ROLES.get(role_name, "generic_worker")
```

If a role name is **not** in `REAL_ACTION_ROLES`, it resolves to `"generic_worker"` and
`_best_match()` genuinely picks the account — a new `AGENT_CAPABILITIES` entry tagged
with that role **will** get real traffic.

If a role name **is** in `REAL_ACTION_ROLES`, it dispatches to that role's own dedicated
module instead. Some of those modules (`code_writers.py`, `reviewer.py`,
`security_scanner.py`, `extraction_table_builder.py`, `note_table_builder.py`,
`content_adapter_pool.py`) still call into `eo/worker_pool.py`'s tag-based selection
internally, so tagging still works for them. **But several others hardcode their own
`CHAIN = [...]` list at the top of the file and never look at `AGENT_CAPABILITIES` at
all** — for those, adding a registry tag changes nothing; the only way to give them a
new account is to edit their `CHAIN` directly.

I traced every module this rollout touches. Verified, from the actual files:

| Role(s) | Resolves via | Real routing mechanism | New registry tag alone works? |
|---|---|---|---|
| `writer`, `editor`, `analyst`, `formatter`, `brainstormer`, `outliner`, `researcher`, `fact_checker` | not in `REAL_ACTION_ROLES` | `generic_worker.run()` → `_best_match()` | **Yes** |
| `report_writer`, `gatekeeper` | not in `REAL_ACTION_ROLES`, **but** `agents/report_writer.py` also defines its own `CHAIN` + `run_report_writer()`, which `eo/registry.py`'s separate legacy `REGISTRY` dict (used by `resolve()`, not `resolve_role()`) still points to | ambiguous — two dispatch paths exist in this codebase; confirm which one your live pipeline actually calls before relying on a tag (see verification command below) | **Verify first** |
| `idea_planner`, `prompt_writer`, `test_writer` | **in** `REAL_ACTION_ROLES` | own `CHAIN` in `agents/idea_planner.py` / `prompt_writer.py` / `test_writer.py` | **No** |
| `structure_architect` | **in** `REAL_ACTION_ROLES` | own `CHAIN` in `agents/structure_architect.py` (currently `GROQ_API_KEY_9` → `GROQ_API_KEY`) | **No** |
| `memory_search`, `duplication_checker` | **in** `REAL_ACTION_ROLES` | both call `utils/embedding.py`'s `embed_text()`, which hardcodes `os.getenv("HUGGINGFACE_API_KEY")` directly — no key parameter at all | **No** |
| `documentation_writer` (documentation_agent.py) | not a real-action-role name itself, but `agents/documentation_agent.py` has its own `CHAIN = [{"provider":"mistral","key_env":"MISTRAL_API_KEY"}]` and is called directly, not through `generic_worker` | own `CHAIN` | **No** |
| `final_qa` | no `agents/final_qa.py` module exists in your repo at all — this role only ever runs through `generic_worker.run()` if something classifies a task as needing it | `generic_worker.run()` → `_best_match()` | **Yes** |
| `verifier` (→`reviewer.py`), `extraction_table_builder`, `note_table_builder`, `source_manager`, `backlink_detector`, `source_planner_lean`, `correction_locator` | `verifier`/`extraction_table_builder` are in `REAL_ACTION_ROLES`, but route to modules with their own `_select_workers()` pulling from `AGENT_CAPABILITIES`; the notes-domain roles resolve to `generic_worker` directly | `worker_pool`/`_best_match`, both real | **Yes** |
| `inspector` | never goes through `AGENT_CAPABILITIES` at all — `eo/inspector.py` has its own hardcoded `CHAIN` (`EO_INSPECTOR_GROQ_KEY_1` → `_2`) | own `CHAIN` | **No** |
| `panel_member_b`, `panel_member_c` | never passed to `_best_match()` anywhere in the codebase — `eo/panel.py`'s tier-0 3-way vote uses its own hardcoded `MEMBER_B_CHAIN` / `MEMBER_C_CHAIN` | own `CHAIN`s | **No** (the existing tags on `EO_PANEL_CEREBRAS_KEY`/`EO_PANEL_GITHUB_PAT` are decorative) |
| `implementer`, `content_writer` | `implementer` is in `REAL_ACTION_ROLES` → `code_writers.py`, which uses `eo/worker_pool.py`'s shared `_select_workers()` | tag-driven, real | **Yes** |

**Verification recipe**, if you ever want to double-check a role yourself before adding
a key for it:

```bash
grep -n "\"<role_name>\"" eo/registry.py   # is it in REAL_ACTION_ROLES?
grep -n "^CHAIN" agents/<its_module>.py    # does that module hardcode its own chain?
grep -n "_select_workers\|generic_worker" agents/<its_module>.py  # or does it use the shared, tag-driven pool?
```

This isn't a criticism of the codebase's design — hardcoded chains for real-action
agents are a deliberate, reasonable choice (comments in the code explain the tradeoffs
each time). It just means **"add a registry tag" is not a universal lever** — for the
dead-tag rows above, the real patch is an edit to that module's own `CHAIN`, and this
guide's patch plan (§5) treats them that way instead of pretending a tag will help.

---

## 2. What's already wired vs. what genuinely needs new code

**Mistral is already a fully working provider** in `utils/llm_client.py` — `_get_mistral()`
and `MISTRAL_BASE_URL = "https://api.mistral.ai/v1"` already exist, it's already in the
`generate_text()` provider-getter dispatch dict, and `agents/generic_worker.py`'s
`PROVIDER_DEFAULT_MODEL` already has a `"mistral"` entry. **Adding new Mistral accounts
to tag-driven pools needs zero client-wiring work — just new `AGENT_CAPABILITIES`
entries** (§4).

**Gemini and Hugging Face chat completions are not wired at all yet.** Neither has a
client getter in `utils/llm_client.py`, neither is in the provider dispatch dict, and
neither has a `PROVIDER_DEFAULT_MODEL` entry. This is real, necessary code — §5's
Patch 1 and Patch 2 build it, using the same OpenAI-SDK-compatible pattern the existing
`_get_mistral()`/`_get_github()` already use:

- Gemini: `base_url="https://generativelanguage.googleapis.com/v1beta/openai/"`
  (confirmed current as of Google's OpenAI-compatibility docs). Models:
  `gemini-2.5-flash-lite`, `gemini-2.5-flash`, `gemini-2.5-pro`.
- Hugging Face: `base_url="https://router.huggingface.co/v1"` (HF's Inference Providers
  router, confirmed current as of HF's own docs). Model IDs need a `:provider` suffix,
  e.g. `"openai/gpt-oss-120b:cerebras"`, or `:auto` to let HF pick — **verify a working
  model:provider pair against `GET https://router.huggingface.co/v1/models` before
  wiring it in**, since which providers serve which models changes over time.

**Hugging Face embeddings** (`memory_search`/`duplication_checker`'s actual need) is a
*different* HF product surface — `utils/embedding.py`'s existing
`https://router.huggingface.co/hf-inference/models/.../feature-extraction` call, not the
chat router above. It already works for one account; giving it real fallback accounts
means adding a `key_env` parameter to `embed_text()` (Patch 7), not touching the chat
wiring at all.

---

## 3. Your actual key inventory and what each is for

From your repo's `env(example).txt`:

| Env vars | Count | Status |
|---|---|---|
| `GEMINI_API_KEY_1` .. `GEMINI_API_KEY_13` | 13 | empty, need real values |
| `MISTRAL_API_KEY_2` .. `MISTRAL_API_KEY_7` | 6 | empty, need real values (existing `MISTRAL_API_KEY` = account 1, already live) |
| `HUGGINGFACE_API_KEY_2` .. `HUGGINGFACE_API_KEY_7` | 6 | empty, need real values (existing `HUGGINGFACE_API_KEY` = account 1, already live) |

25 new accounts total. Get every Hugging Face token as **Fine-grained → Inference
preset only** — narrower than Full Access, and the only scope any of these calls
actually need. If you ever route through a dedicated Inference Endpoint instead of
serverless models, double check the endpoint-access box under Custom scopes — the
Inference preset alone may only cover serverless models.

---

## 4. Final allocation plan

Grounded in §1's table — only pools where a registry tag actually routes traffic get
new `AGENT_CAPABILITIES` entries. Everything else gets a direct `CHAIN` edit in §5.

### 4a. Tag-driven roles — new `AGENT_CAPABILITIES` entries (Patch 3)

| New key | Provider / model | `natural_roles` | Why |
|---|---|---|---|
| `GEMINI_API_KEY_1` | gemini, `gemini-2.5-flash-lite` | `["formatter", "outliner", "brainstormer"]` | fast/simple shaped roles from the old single-`GROQ_API_KEY` catch-all |
| `GEMINI_API_KEY_2` | gemini, `gemini-2.5-flash` | `["writer", "editor", "researcher"]` | mid-weight roles from the same catch-all |
| `GEMINI_API_KEY_3` | gemini, `gemini-2.5-pro` | `["analyst", "fact_checker", "final_qa"]` | heavier-reasoning roles; also gives `final_qa` its first-ever second account |
| `MISTRAL_API_KEY_2`, `_3` | mistral, `mistral-large-latest` | `["writer", "editor"]` | real siblings — these two roles genuinely resolve to `generic_worker`, unlike `documentation_writer` (§5, Patch 5) |
| `MISTRAL_API_KEY_4` | mistral, `mistral-large-latest` | `["final_qa"]` | second real candidate for this role alongside `GEMINI_API_KEY_3` |
| `HUGGINGFACE_API_KEY_2`, `_3` | huggingface, small fast model via router (pick one and confirm at `/v1/models`, e.g. `Qwen/Qwen2.5-7B-Instruct:auto`) | `["source_manager", "backlink_detector"]` | patches the 9-role/5-account extraction pool (`GROQ_API_KEY_6/7/8` + reserves) — this pool's roles resolve to `generic_worker` or use `worker_pool`, both real |
| `HUGGINGFACE_API_KEY_4`, `_5` | huggingface, same model choice | `["source_planner_lean", "correction_locator", "note_table_builder"]` | same extraction pool, remaining roles |

That leaves `GEMINI_API_KEY_4` through `_13`, `MISTRAL_API_KEY_5/6/7`, and
`HUGGINGFACE_API_KEY_6/7` for the dead-tag pools below, plus a genuine reserve.

### 4b. Dead-tag pools — direct `CHAIN` edits (Patch 4–8)

| Keys | Target module | What changes |
|---|---|---|
| `GEMINI_API_KEY_4`, `_5`, `_6` | `agents/structure_architect.py` | append as real fallback steps after `GROQ_API_KEY_9`/`GROQ_API_KEY` — this role has had exactly one real account (plus one shared-pool fallback) this whole time |
| `GEMINI_API_KEY_8`, `MISTRAL_API_KEY_5` | `agents/documentation_agent.py` | append as fallback steps after `MISTRAL_API_KEY` — right now this agent has a single account with zero fallback |
| `GEMINI_API_KEY_10`, `_11` | `eo/inspector.py` | append as fallback steps after `EO_INSPECTOR_GROQ_KEY_1`/`_2` — this runs on **every incoming task**, currently backed by 2 Groq accounts only |
| `GEMINI_API_KEY_7`, `MISTRAL_API_KEY_6` | `eo/panel.py`'s `MEMBER_B_CHAIN` / a new `MEMBER_D` slot, or decide to retire the fixed A/B/C shape | this is a real design decision, not a wiring one — see the callout below |
| `HUGGINGFACE_API_KEY_6`, `_7` | `utils/embedding.py` + `agents/memory_search.py` + `agents/duplication_checker.py` | give `embed_text()` a real fallback chain — this is the only path with **zero** redundancy today (a single hardcoded env var, no fallback of any kind) |

**Reserve, untagged:** `GEMINI_API_KEY_9`, `_12`, `_13`, `MISTRAL_API_KEY_7`. Don't
assign these yet — they're for whatever you decide to do with the panel-voice question
below, plus genuine slack once you see real quota data.

**Callout — the panel_member_b/c decision:** `eo/panel.py`'s tier-0 classification vote
is a fixed 2-member chain (`MEMBER_B_CHAIN` = Cerebras, `MEMBER_C_CHAIN` = GitHub
Models) that runs on every incoming task, separate from everything else in this guide.
Adding Gemini/Mistral here is not "give this pool a fallback sibling" the way it is for
`structure_architect` — it's "should the panel have a 3rd or 4th voting member." That
changes vote-tallying logic in `_get_member_vote()`/wherever the votes are combined, not
just the chain list. Decide this deliberately before touching it; it's flagged, not
prescribed, here.

---

## 5. Step-by-step patch rollout

Small, independently-applicable, independently-testable steps. Ask for each patch file
by number when you're ready for it — I'll generate the actual diff against your repo
and you apply + test before moving to the next one. Suggested order (each one builds on
the last, but 3 and 4a are safe to reorder if you want extraction-pool redundancy
before structure_architect's, or vice versa):

| # | Patch | Files touched | Unlocks |
|---|---|---|---|
| **1** | Wire Gemini as a provider | `utils/llm_client.py` (client getter + base_url + provider dispatch entry + `QUOTA_CONFIG` line), `agents/generic_worker.py` (`PROVIDER_DEFAULT_MODEL["gemini"]`) | every Gemini-based step below |
| **2** | Wire Hugging Face chat as a provider | same two files as Patch 1, HF-specific | every HF-chat-based step below (not embeddings — that's Patch 7) |
| **3** | Tag-driven registry entries | `eo/registry.py` (`AGENT_CAPABILITIES` additions from §4a) | real fallback for the catch-all + extraction pools, zero other code changes |
| **4** | `structure_architect` fallback chain | `agents/structure_architect.py` | this role survives a `GROQ_API_KEY_9` + `GROQ_API_KEY` outage for the first time |
| **5** | `documentation_agent` fallback chain | `agents/documentation_agent.py` | same, for docs generation |
| **6** | `inspector` fallback chain | `eo/inspector.py` | triage survives a 2-account Groq outage |
| **7** | `embed_text()` real fallback | `utils/embedding.py`, `agents/memory_search.py`, `agents/duplication_checker.py` | semantic search / dup-checking survive an HF outage — currently the single weakest link in the whole system |
| **8** | Panel-voice decision (only after you decide §4b's callout) | `eo/panel.py` | optional — a genuine 3rd/4th distinct-lineage opinion in the tier-0 vote |
| **9** | `QUOTA_CONFIG` cleanup pass | `utils/llm_client.py` | dashboard shows real (or honestly-absent) daily limits for gemini/mistral/huggingface instead of `None` |

Each patch will come with the exact commands to verify it (a one-off test call through
`generate_text()` or the specific agent's `run()`, and what a successful log line looks
like) so you can confirm it actually works before moving on, not just that it imports
cleanly.

---

## 6. Quota tracking

`utils/llm_client.py`'s real `QUOTA_CONFIG` is a flat `{provider: daily_request_limit}`
dict (not the nested per-model shape you may have seen sketched elsewhere — that
doesn't exist in this codebase, and building it would be a separate, larger change to
`log_usage()`/the dashboard, not part of this rollout). Patch 9 just adds honest
entries:

```python
QUOTA_CONFIG = {
    "groq": 14400,
    "cerebras": 14400,
    "github": 150,
    "gemini": None,        # fill in once you've confirmed your actual tier's daily cap
    "huggingface": None,   # HF's serverless router doesn't publish a stable RPD either
}
```

`None` is the honest choice over a guessed number — `page.js` already renders a missing
`daily_limit` gracefully (raw token/request count instead of a percentage bar).

---

## 7. Rollout checklist

1. [ ] Fill in the 25 real key values in `.env` (§3).
2. [ ] Patch 1 — wire Gemini. Verify: one `generate_text()` call with a
       Gemini-only chain returns text and a `usage_update` event fires.
3. [ ] Patch 2 — wire Hugging Face chat. Same verification, HF-only chain.
   Confirm a real `model:provider` string works against
   `GET https://router.huggingface.co/v1/models` first.
4. [ ] Patch 3 — add the tag-driven `AGENT_CAPABILITIES` entries. Verify:
       force a `structure_architect`-adjacent generic role and confirm the
       new account shows up in the usage dashboard after a call.
5. [ ] Patch 4 — `structure_architect` chain. Verify: temporarily unset
       `GROQ_API_KEY_9`/`GROQ_API_KEY` and confirm the run still succeeds via Gemini.
6. [ ] Patch 5 — `documentation_agent` chain. Same kind of forced-failure test.
7. [ ] Patch 6 — `inspector` chain. Same.
8. [ ] Patch 7 — `embed_text()` fallback. Verify: unset `HUGGINGFACE_API_KEY`
       and confirm `memory_search`/`duplication_checker` fall through to
       `HUGGINGFACE_API_KEY_6` instead of raising.
9. [ ] Decide the panel-voice question (§4b callout); apply Patch 8 only if you
       want it.
10. [ ] Patch 9 — quota config cleanup.
11. [ ] Re-check the usage dashboard in ~2-4 weeks. Watch the extraction pool and
        `embed_text()` fallback specifically — those had the least redundancy of
        anything in this rollout before today.
