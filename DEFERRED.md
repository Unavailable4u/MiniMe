# Deferred ideas

Running log of things explicitly flagged as "not now" during the
CLI-as-Internal-Interface work (see
`MiniMe-CLI-Internal-Architecture-Plan.md` and its patch breakdown,
`MiniMe-Patch-Series-B-Plan.md`), kept in one place so reviewing
everything deferred is one glance instead of grepping the whole
codebase.

Convention: when a patch flags something inline as out of scope, add a
one-line entry here in the same commit, pointing back to the inline
comment (`file:line`) that flagged it.

## Deferred at the start of Patch Series B (architecture plan §3.6)

- **The daemon (external project access).** Separate feature, separate
  risk profile — touches a person's own filesystem outside this
  system, not just internal capability access. Comes later, with its
  own hard ceiling on what it can run.
- **Write/execute capability on the backend's own code.** Deferred
  until the atomicity/isolation problem in the architecture plan's §4
  (live bind-mounted `./backend:/app`, interrupted-write risk) has a
  real design answer — atomic writes (temp file + validate + swap) and
  probably a verify-then-promote working copy, neither designed yet.
- **Anything beyond architecture plan §3.1–§3.5.** No formal scope
  boundary drawn yet for letting agents write, call MCP tools
  directly, or manage their own skill library. Flag each such idea
  inline as it comes up during the build; this file is the rollup.
- **Drift-check mechanism for the capability/security layer.** Not
  designed yet — a periodic check that the skill-entry capability/
  redaction layer (Patch B1/B2) still matches reality. Worth deciding
  once that layer exists and has real content, not before.

<!-- New entries go below this line, oldest first. -->
