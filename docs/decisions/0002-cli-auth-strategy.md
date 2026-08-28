# 0002 — CLI Auth Strategy

**Status:** superseded — the human-facing CLI (`cli/minime_cli/`) was
removed in Patch C0; see `MiniMe-Patch-Series-C-Plan.md`, Track 1.
Left here as the historical record of why A6 was built the way it was.

**Status (as originally decided):** decided, implemented in
`cli/minime_cli/auth.py` (Patch A6).

## The question

Patch A6 (`MiniMe_Implementation_Guide_CLI_Skills_MCP_Memory.md`, Part
1) flags CLI auth as the one real open design question, and lists two
existing mechanisms in this codebase, neither a perfect fit as-is:

- `_verify_supabase_jwt` (`backend/api/deps.py`) -- built for a
  logged-in browser session.
- The daemon's shared pairing token (`daemon/config.py`) -- built for
  one machine pairing to one workspace, not a general chat session.

And two options to choose between:
- (a) a CLI login flow that obtains a real Supabase session/JWT the
  same way the browser does, cached locally after first login.
- (b) a new lightweight personal-access-token mechanism, needing its
  own migration + issuance endpoint.

## What we found before choosing

`frontend/app/context/AuthContext.jsx` signs in with
`supabase.auth.signInWithPassword({ email, password })` --
this project already relies on Supabase's password grant for login,
not an OAuth/magic-link-only flow. That grant is a plain, unauthenticated
POST to Supabase's own Auth REST API
(`{SUPABASE_URL}/auth/v1/token?grant_type=password`), authenticated
with the project's public anon key -- nothing about it is
browser-specific. `backend/scripts/get_test_jwt.py`'s `sign_in()`
already calls this exact endpoint to mint test JWTs for hitting
`require_auth()`-protected routes without a browser, which is direct,
existing proof this call shape works against this project's Supabase
instance end to end.

`api/deps.py`'s `require_auth()` verifies *any* correctly-signed
Supabase JWT via the project's JWKS endpoint -- it has no notion of
"came from the browser" baked in. A token minted by a CLI's own
password-grant call is byte-for-byte the same kind of token, with the
same `sub` claim, that a browser-obtained one is.

## Decision: option (a)

The CLI signs in directly against Supabase's Auth REST API using the
password grant -- the exact same grant the web frontend already uses,
just called from Python instead of `supabase-js`. The resulting
`access_token` is sent as `Authorization: Bearer <token>` on every
backend request, and round-trips through the *existing*,
*unmodified* `require_auth()` exactly like a browser session would.

**Why not (b):** a personal-access-token mechanism would need its own
migration, its own issuance/revocation endpoints, and its own
verification path alongside `require_auth()` -- new surface area to
build and keep secure, for a problem the project's existing auth setup
already solves for free. Nothing about "a terminal instead of a
browser" changes what a valid session is; option (a) needed zero
backend changes.

## What the CLI needs, and never touches

- `MINIME_SUPABASE_URL` / `MINIME_SUPABASE_ANON_KEY` -- the same
  values as the frontend's `NEXT_PUBLIC_SUPABASE_URL` /
  `NEXT_PUBLIC_SUPABASE_ANON_KEY`. Public by design (Supabase's own
  docs describe the anon key as safe to ship in a browser bundle);
  safe to keep in `~/.minime/config.json`.
- **Never** `SUPABASE_SERVICE_ROLE_KEY`. The CLI is a client, not a
  trusted backend process -- it has no more privilege than the browser
  does, and no code path in `cli/` reads or accepts that key.

## Local caching

`cli/minime_cli/auth.py` caches `{access_token, refresh_token,
expires_at}` at `~/.minime/credentials.json` (mode `0600`) after
login, and silently refreshes via Supabase's `refresh_token` grant
when the cached token is near expiry -- mirroring what `supabase-js`'s
client already does in the background for the browser session. A
refresh failure (dead/rotated-out refresh token) surfaces as "session
expired, run `minime login` again" rather than a raw HTTP error.

## Follow-on implication for A7

Patch A7 (`minime attach`) reuses the *daemon's* existing pairing flow
unchanged -- that flow authenticates a *machine* to a *workspace*, a
different concern from authenticating a *person* to the *chat API*
this decision covers. The two mechanisms coexist deliberately; A7
should not attempt to unify them.
