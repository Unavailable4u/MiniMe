# promptfoo evals — Master Guide D2

Free, open-source, YAML-defined regression tests for the Role Library's
prompts (`eo/registry.py`'s `ROLE_PROMPTS_*`). Wired into CI (B3) as its
own job — see `.github/workflows/ci.yml`.

## Why this isn't just YAML pointing at static prompt files

Role briefs in this codebase are **not** files. They live in Upstash
Redis under the `registry:role_prompts` key, seeded from
`eo/registry.py`'s `ROLE_PROMPTS_SEED` on first read and mutable at
runtime (Panel brief-writer edits, `PUT /api/roles/{role_name}`, etc.).
A promptfoo config that hardcoded prompt text would silently drift from
what the app actually hires the moment someone edits a brief through the
UI.

So instead of a `prompts:` list of raw strings, every case here runs
through **`providers/role_provider.py`** (added in the next patch), a
custom promptfoo Python provider that:

1. Calls `eo.registry.get_role_prompt(role_name)` to fetch the *live*
   brief — same call `agents/generic_worker.py`'s `run()` makes.
2. Runs it through `utils/llm_client.generate_text()` using that role's
   real fallback chain — same code path production traffic uses, not a
   reimplementation.

This means a promptfoo run is always testing the current, real prompt —
not a stale copy — at the cost of needing Redis reachable (see below).

## Running locally

```bash
cd backend/evals/promptfoo
npm install
npm run eval        # opens results in terminal
npm run view         # optional: local web UI over past runs
```

Needs the same backend env vars the app itself needs to reach the Role
Library and the LLM providers under test — point this at your `.env` (or
export the vars directly): `UPSTASH_REDIS_REST_URL`,
`UPSTASH_REDIS_REST_TOKEN`, plus whichever provider keys the cases you're
running exercise (e.g. `GROQ_API_KEY`, `CEREBRAS_API_KEY_1`,
`MISTRAL_API_KEY`).

## Running in CI

CI does **not** have access to the same Redis instance as your local
dev/prod environment unless you add `UPSTASH_REDIS_REST_URL` /
`UPSTASH_REDIS_REST_TOKEN` as repo secrets. Until you do, `role_provider.py`
falls back to `ROLE_PROMPTS_SEED` for any role with no reachable live
entry (logged as a warning in the eval output, not a failure) — so CI
still runs, just against seed briefs instead of whatever's live. Wiring
real secrets in is a deliberate opt-in, covered in the CI patch, not
assumed here.

## Layout (filled in by later patches in this series)

```
backend/evals/promptfoo/
  package.json
  README.md                  (this file)
  promptfooconfig.yaml        # main config: providers + prompts + tests
  providers/
    role_provider.py          # bridges promptfoo -> live Role Library + llm_client
  tests/
    output_organizer.yaml     # CO1's dedicated case set (Guide's explicit call-out)
    <role_name>.yaml           # one file per actively-iterated role
  compare/
    providers.promptfooconfig.yaml   # head-to-head provider comparison config
```
