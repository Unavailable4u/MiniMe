# 0001 — Scope boundaries for the CLI + Skills + MCP work

**Status:** accepted
**Context:** Implementation Guide — CLI + Skills + MCP, and Memory + Personalization, Section 0 and Patch A0.

## Decision 1 — Local file/shell capability is not rebuilt as MCP

`daemon/` already provides a working, tested local file and shell
capability, gated by an explicit propose/confirm safety step:

- `daemon/path_guard.py`, `daemon/config.py`, and `daemon/connection.py`
  give the daemon a containment boundary and a reconnecting websocket
  link to the backend.
- `daemon/tools.py` exposes `list_dir` and `read_file` as read-only,
  run-freely tools, and `write_file`, `delete`, and `execute_command`
  as mutating tools.
- `backend/eo/local_workspace_tools.py` already implements the
  propose/confirm gate: `propose_action()` validates and stores a
  mutating call as pending, and a separate `confirm_action()` (or
  `deny_action()`) step is required before it runs. The read-only
  path is exempt and runs freely. Every proposed, confirmed, or
  denied call is logged as an event.

This is, in substance, the "Filesystem MCP" and "Shell/Terminal MCP"
capability the long-term vision describes — and its safety model is
already stricter than a generic community MCP server would give out
of the box.

**We will not build a second, parallel local file/exec tool path
using the Model Context Protocol or any other mechanism.** Any new
local-machine capability is added as a new tool in `daemon/tools.py`,
routed through `local_workspace_tools.py`'s existing propose/confirm
machinery. The true external MCP client introduced later in this
guide (Patch A1) is for connecting to *third-party* MCP servers
(GitHub, Context7, web search, etc.) — it is not, and must not become,
a replacement for what the daemon already does.

## Decision 2 — Skills stay in one store

`backend/eo/skill_library.py` is a real, already-integrated skill
system, not a placeholder:

- `get_relevant_skill(task_text)` does semantic lookup against every
  stored skill's own embedding and returns the best match's doc text
  once it clears `SKILL_MATCH_THRESHOLD` (0.75), or `""` on a miss.
  That empty-string result is deliberately load-bearing — a future
  self-improvement loop uses it as the signal that a task type is
  unfamiliar and needs research.
- `write_skill(title, doc_text, source)` persists a skill both as a
  raw record (`registry:skill_library`, via `memory/bus.py`) and as a
  vector embedding, keyed by a slug of the title so re-writing a title
  updates in place.
- The store is seeded once from `SKILL_SEED`; after that the live
  store in Redis/Vector is authoritative.
- It is already wired into `backend/agents/generic_worker.py`.

**A CLI or MCP-facing "skills" feature is a read-facing view onto this
existing store — it does not create a second, file-based
`SKILL.md`-per-folder system.** `registry:skill_library` remains the
single source of truth. If a human-editable-file workflow is wanted
later, it is built as an import/export layer on top of this store,
not as a replacement for it.

## Why this is recorded

Every later patch in the CLI + Skills + MCP track (A1 through A8)
assumes both decisions above. Writing them down here means a fresh
session — or a future contributor without this conversation's
context — can check this file before proposing a new local-exec
mechanism or a second skill store, instead of re-litigating a
question this document already settled.
