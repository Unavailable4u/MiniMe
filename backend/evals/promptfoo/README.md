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

## Two configs, not one

`role_provider.py` and `output_organizer_provider.py` take incompatible
shapes -- (role_name, task_text) -> str for one, (role_outputs,
user_request) -> {answer, dedup_notes} for the other. promptfoo runs
every test case's vars against every configured provider, so mixing both
into one `providers:` list would run Role Library cases through the
organizer provider (and vice versa) and fail confusingly. They get their
own configs and their own npm scripts instead:

```bash
npm run eval             # Role Library prompts (promptfooconfig.yaml)
npm run eval:organizer   # output_organizer synthesis (output_organizer.promptfooconfig.yaml)
```

See `providers/output_organizer_provider.py`'s module docstring for the
full reasoning.

## Provider comparison (patch 5, opt-in, not run in CI)

A third config, `compare/providers.promptfooconfig.yaml`, runs the
Role Library's case files against Groq, Cerebras, Mistral, and Gemini
side by side, using the same `providers/role_provider.py` -- not a
separate/reimplemented provider script. It works by declaring four
`providers:` entries that all point at `role_provider.py`, each with a
different `provider_override` in its `config:`; `role_provider.py`
honors that by building a single forced one-step chain for that
provider instead of its normal quota-ranked live chain (see that
file's own "D2 patch 5" comment for the mechanism). promptfoo's
ordinary test-case x provider matrix does the actual side-by-side
comparison automatically once multiple `providers:` entries exist --
nothing else needed.

```bash
npm run compare        # runs tests/contradiction_detector.yaml (or
                        # whatever's wired into compare/providers
                        # .promptfooconfig.yaml's tests:) against all
                        # four providers, one column per provider
```

Deliberately **not** wired into CI (patch 6) -- this quadruples LLM
spend per run (one call per provider per case) for a comparison you'd
actually want to read and think about, not a pass/fail gate. Run it by
hand when you're deciding which provider handles a role best, not on
every push.

Add a role to the comparison by adding its case file to that config's
`tests:` list -- same generic_worker-routing rule as everywhere else in
this package applies (`provider_override` changes which provider a
role's chain uses, not whether role_provider.py can run that role at
all -- see `providers/role_provider.py`'s module docstring / this
package's `tests/_template.yaml`).

Only Groq/Cerebras/Mistral/Gemini are wired up (all four are
OpenAI-SDK-shaped in `utils/llm_client.py`, so they share the same
step shape). Cloudflare and HuggingFace aren't included -- Cloudflare
in particular needs a different step shape (`account_id_env` +
`token_env` instead of a single `key_env`). See
`providers/role_provider.py`'s `PROVIDER_OVERRIDE_DEFAULTS` comment for
how to add either.

## Layout (filled in by later patches in this series)

```
backend/evals/promptfoo/
  package.json
  README.md                            (this file)
  promptfooconfig.yaml                 # Role Library config: role_provider.py + tests/<role_name>.yaml
  output_organizer.promptfooconfig.yaml   # output_organizer's own config, see "Two configs" above
  providers/
    role_provider.py                   # bridges promptfoo -> live Role Library + llm_client
    output_organizer_provider.py       # bridges promptfoo -> agents/output_organizer.py directly
  tests/
    output_organizer.yaml              # CO1's dedicated case set (Guide's explicit call-out)
    asserts/
      output_organizer_asserts.py      # custom python asserts for the {answer, dedup_notes} contract
    <role_name>.yaml                    # one file per actively-iterated role (Role Library config)
  compare/
    providers.promptfooconfig.yaml     # head-to-head provider comparison config
```
