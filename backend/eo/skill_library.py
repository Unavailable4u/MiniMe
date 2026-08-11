"""
eo/skill_library.py — Part 6 §E2: Skill library (task 14 of the build
order).

A folder of short "how to do X" docs agents read before attempting an
unfamiliar task type. Same store/retrieve split as eo/routing_memory.py
(one Upstash Vector index, its own id prefix so a query here never
collides with routing_memory.py's outcome log or agents/
duplication_checker.py's code-similarity index), plus a
seed-then-live-store bootstrap the exact shape eo/registry.py's
ROLE_PROMPTS_SEED already established for the Role Library: a small
hand-written dict is the starting content on a fresh system, the
`registry:skill_library` Redis key is the real source of truth after
that seed has been written once.

`registry:` prefix is deliberate, not incidental — memory/bus.py's own
_namespaced() already exempts every `registry:`-prefixed key from
per-app_slug namespacing ("the role-prompt and role-to-agent registries
are also properties of the SYSTEM, not any one project"). A skill doc
("how do you extract structured fields from a set of sources") is the
same kind of system-wide property, not something that should fragment
per-project the way module_specs/current_plan correctly do.

Two jobs:
  get_relevant_skill(task_text) — semantic lookup against every stored
      skill's own embedding, returns the best match's doc text if it
      clears SKILL_MATCH_THRESHOLD, "" otherwise. That empty-string
      result is deliberately load-bearing, not just "no match found" —
      task 14's self-improvement loop (a later patch) treats an empty
      return here as the actual signal that this task type is
      unfamiliar and worth researching + writing a new skill for.
  write_skill(title, doc_text, source) — persists a new (or edited)
      skill: raw record into the registry:skill_library dict (so it can
      be listed/edited later, same as Role Library's brief store) AND
      embedded into Vector (so get_relevant_skill() can actually find
      it by similarity). skill_id is derived from title, not
      auto-incremented, so re-writing the same title's skill (e.g. a
      human edit after reviewing a self-improvement-loop draft) updates
      the existing entry in place instead of creating a duplicate the
      way a UUID-per-write would.

ASSUMPTION FLAGGED, same posture eo/source_index.py's own docstring
takes when a spec detail isn't pinned down elsewhere: SKILL_SEED below
is a small, genuinely useful starting set, but "your trickiest task
types" is inherently specific to how you actually use the app — treat
these three as placeholders to edit/replace, not as the definitive
seed. Editing SKILL_SEED (or calling write_skill() directly) is the
whole mechanism; nothing else needs to change to add more.

Wiring this into agents/generic_worker.py's system_prompt construction,
and the self-improvement loop itself (research-on-miss -> write_skill),
are later patches — this module is deliberately usable and independently
testable on its own first, same incremental order the extraction-table
fix already followed (data layer correct before call sites are rewired
to depend on it).

Place this file at: eo/skill_library.py
"""
import os
import sys
import re
import time

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import read, write, vector_index
from utils.embedding import embed_text

# Own id-prefix namespace, same reasoning eo/routing_memory.py's own
# ID_PREFIX comment gives: never collide with routing_memory.py's
# "eo_outcome" prefix or agents/duplication_checker.py's own, even
# though all three share the one Upstash Vector index.
ID_PREFIX = "skill"

SKILL_LIBRARY_KEY = "registry:skill_library"

# Cosine similarity (this Vector index's own configured metric — see
# utils/embedding.py's own comment on why COSINE/384-dim was chosen).
# Deliberately lower than agents/duplication_checker.py's
# SIMILARITY_THRESHOLD = 0.90 — that constant is asking "is this
# functionally the same artifact," this one is asking "is this task
# close enough in KIND to a known task that its how-to doc still
# applies," a much looser bar. Starting guess, not a measured value —
# tune against real retrieval misses/false-positives once this is
# live, the same "worth deciding, not guessing forever" flag this
# codebase already raises elsewhere (see T2's undo/version-history
# note in the build guide).
SKILL_MATCH_THRESHOLD = 0.75

# What used to be the live skill set is now only a SEED — the starting
# contents on a totally fresh system, before the self-improvement loop
# (a later patch) has ever written anything of its own. After the first
# read, registry:skill_library (Redis) is the real source of truth;
# this dict is never read again except to bootstrap. Exact mirror of
# eo/registry.py's own ROLE_PROMPTS_SEED comment, one level up (skills
# instead of role briefs).
#
# ASSUMPTION FLAGGED — see module docstring: placeholders for your own
# trickiest task types, not a definitive set.
SKILL_SEED = {
    "structured_field_extraction": {
        "title": "Extracting structured fields from a set of sources",
        "doc": (
            "When asked to pull specific named fields (e.g. sample size, "
            "methodology, a price, a spec value) out of a set of sources "
            "rather than writing a free-form summary: extract each field "
            "independently per source, use null/None for any field the "
            "source genuinely doesn't state rather than inferring or "
            "estimating one, and never merge two different sources' "
            "values into a single answer even if they discuss the same "
            "topic. Keep the output one row per source so a caller can "
            "trace every value back to where it came from."
        ),
        "source": "hand_written",
    },
    "domain_scoped_web_research": {
        "title": "Running a domain-scoped web research pass",
        "doc": (
            "When a task needs current, real information from the open "
            "web rather than general knowledge (news, product/community "
            "opinion, forum discussion, current events): pick the "
            "narrowest domain scope that actually covers the question "
            "(a forum/Reddit scope for opinion and discussion, a news "
            "allowlist for current events, Hacker News for tech/startup "
            "discussion, general web only when nothing narrower fits) "
            "before falling back to an unscoped search. Report only "
            "sources that actually came back from the search — never "
            "state a fact as if it came from a search that wasn't "
            "actually run, and never paraphrase a source you didn't "
            "really retrieve."
        ),
        "source": "hand_written",
    },
    "multi_source_synthesis": {
        "title": "Synthesizing an answer across several sources that disagree",
        "doc": (
            "When multiple sources touch the same question and don't "
            "fully agree: state each source's actual position rather "
            "than silently picking one and presenting it as the "
            "consensus, note where they disagree and why if that's "
            "apparent (different methodology, different scope, different "
            "date), and don't manufacture a false middle-ground answer "
            "just to resolve the disagreement. A real, named "
            "disagreement is a more useful answer than a smoothed-over "
            "one."
        ),
        "source": "hand_written",
    },
}


def _slug(title: str) -> str:
    """Deterministic id from a title -- same slugify shape
    memory/bus.py's own slugify() uses for app slugs, kept local here
    rather than imported since this only needs the one call and
    memory/bus.py's version is tuned for app-name slugs specifically
    (different max_len default, different call sites)."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.strip().lower()).strip("_")
    return slug[:60] or f"skill_{int(time.time() * 1000)}"


def _load_skills() -> dict:
    """Bootstraps from SKILL_SEED exactly once -- first-ever call on a
    fresh system writes the seed into registry:skill_library and
    returns it; every call after that just reads the live store. Same
    read-through-bootstrap shape eo/registry.py's own _load_prompts()
    uses for ROLE_PROMPTS_SEED."""
    existing = read(SKILL_LIBRARY_KEY, default=None)
    if existing is not None:
        return existing
    seeded = {
        skill_id: {**entry, "updated_at": None, "times_matched": 0}
        for skill_id, entry in SKILL_SEED.items()
    }
    write(SKILL_LIBRARY_KEY, seeded)
    return seeded


def list_skills() -> dict:
    """Every stored skill, keyed by skill_id -- {title, doc, source,
    updated_at, times_matched}. Read path for a future Skill Library UI
    panel (mirroring Role Library's own GET), not required by
    get_relevant_skill()/write_skill() below, which only ever touch
    Vector for the hot retrieval path."""
    return _load_skills()


def write_skill(title: str, doc_text: str, source: str = "hand_written") -> str:
    """Persists a skill doc: raw record into registry:skill_library
    (Redis, listable/editable later) AND embedded into Vector (so
    get_relevant_skill() can actually retrieve it by similarity).

    skill_id is derived from `title` (see _slug()) rather than a fresh
    UUID per call, so calling this again with the same title -- a human
    editing a self-improvement-loop draft, or the loop itself refining
    an existing skill -- updates that one entry in place instead of
    silently accumulating near-duplicate skills with the same title.

    Best-effort on the Vector half, same posture eo/routing_memory.py's
    own log_outcome() takes: the Redis write always happens; if
    HF/Vector embedding fails, the skill is saved but not yet
    retrievable by get_relevant_skill() until a future write succeeds
    -- a degraded state, not a crash, for the same reason a missing
    feedback signal in routing_memory.py isn't worth raising over.

    Returns the skill_id.
    """
    skill_id = _slug(title)
    skills = _load_skills()
    existing = skills.get(skill_id, {})
    skills[skill_id] = {
        "title": title,
        "doc": doc_text,
        "source": source,
        "updated_at": time.time(),
        "times_matched": existing.get("times_matched", 0),
    }
    write(SKILL_LIBRARY_KEY, skills)

    try:
        vector = embed_text(f"{title}\n{doc_text}"[:4000])
        vector_index().upsert(
            vectors=[(f"{ID_PREFIX}:{skill_id}", vector, {
                "skill_id": skill_id, "title": title,
            })]
        )
    except Exception as exc:
        print(f"  [Skill Library] embed/upsert skipped for {skill_id!r} "
              f"({exc.__class__.__name__}: {exc}) -- skill saved to Redis "
              f"but not yet retrievable by similarity.")

    return skill_id


def _bump_times_matched(skill_id: str) -> None:
    """Best-effort usage counter, same "read, bump, write back" shape
    eo/registry.py's record_role_hire() uses for times_hired -- a
    dashboard/prioritization signal, not load-bearing for retrieval
    itself, so a failure here is swallowed by get_relevant_skill()'s own
    try/except rather than given its own."""
    skills = _load_skills()
    if skill_id in skills:
        skills[skill_id]["times_matched"] = skills[skill_id].get("times_matched", 0) + 1
        write(SKILL_LIBRARY_KEY, skills)


def get_relevant_skill(task_text: str) -> str:
    """Returns the best-matching skill's doc text if its similarity to
    `task_text` clears SKILL_MATCH_THRESHOLD, "" otherwise -- including
    on any embed/query failure, same "degrade to empty, never raise"
    posture eo/routing_memory.py's own retrieve_similar_outcomes() takes,
    since a caller building a system prompt shouldn't break over a
    Vector hiccup.

    The "" case is NOT just "nothing to add" -- a later patch (task 14's
    self-improvement loop) treats an empty return here as the actual
    signal that this task type has no matching skill yet and is worth
    researching. Bootstraps the seed via _load_skills() first so a
    totally fresh system's hand-written SKILL_SEED is guaranteed to be
    in Vector before the very first query would need it -- see
    _ensure_seed_embedded() below.
    """
    if not (task_text or "").strip():
        return ""

    _ensure_seed_embedded()

    try:
        vector = embed_text(task_text[:4000])
        matches = vector_index().query(
            vector=vector, top_k=1, include_metadata=True,
            filter=f"skill_id != ''",
        )
    except Exception as exc:
        print(f"  [Skill Library] retrieval skipped ({exc.__class__.__name__}: {exc}).")
        return ""

    if not matches or matches[0].score < SKILL_MATCH_THRESHOLD:
        return ""

    skill_id = (getattr(matches[0], "metadata", None) or {}).get("skill_id")
    skills = _load_skills()
    entry = skills.get(skill_id)
    if not entry:
        # Vector and Redis disagreed (e.g. a skill was deleted from
        # Redis but its embedding wasn't cleaned up) -- treat as a miss
        # rather than returning a stale/missing doc.
        return ""

    _bump_times_matched(skill_id)
    return entry["doc"]


_seed_embedded_this_process = False


def _ensure_seed_embedded() -> None:
    """One-time-per-process check that SKILL_SEED's entries actually
    exist in Vector, not just Redis -- _load_skills() bootstraps the
    Redis dict on first read, but writing the Redis record and
    embedding it into Vector are two different calls (write_skill()
    normally does both together; the seed's first-ever bootstrap in
    _load_skills() only does the Redis half, to keep that function
    synchronous and side-effect-light for every caller, not just the
    first). This closes that gap for get_relevant_skill()'s own read
    path specifically, the only path that actually needs the seed to be
    queryable.

    Module-level flag, not a Redis-backed one: worst case on a fresh
    multi-process deployment is a few redundant embed calls across
    processes in the first few minutes, same acceptable-redundancy
    trade-off eo/panel.py's own cold-start paths already accept
    elsewhere rather than adding a distributed lock for a one-time
    bootstrap.
    """
    global _seed_embedded_this_process
    if _seed_embedded_this_process:
        return
    _seed_embedded_this_process = True

    skills = _load_skills()
    for skill_id, entry in skills.items():
        if entry.get("source") != "hand_written" or entry.get("updated_at"):
            # Only the untouched seed needs this backfill -- anything
            # already written via write_skill() (updated_at set) already
            # went through its own embed/upsert call there.
            continue
        try:
            vector = embed_text(f"{entry['title']}\n{entry['doc']}"[:4000])
            vector_index().upsert(
                vectors=[(f"{ID_PREFIX}:{skill_id}", vector, {
                    "skill_id": skill_id, "title": entry["title"],
                })]
            )
        except Exception as exc:
            print(f"  [Skill Library] seed embed skipped for {skill_id!r} "
                  f"({exc.__class__.__name__}: {exc}).")


if __name__ == "__main__":
    # Manual smoke test -- mirrors eo/routing_memory.py's own __main__
    # block. Writes one new skill, then queries something that should
    # match a seed skill and something that plausibly shouldn't match
    # anything yet.
    import json

    sid = write_skill(
        "Writing a fallback chain across multiple provider accounts",
        "When one account/provider might be rate-limited or down, build "
        "a short ordered chain of (provider, model, key_env) steps and "
        "try each in turn, logging which step actually succeeded rather "
        "than assuming the first one always will.",
        source="manual_test",
    )
    print("wrote skill:", sid)
    print("match for 'extract sample sizes from these ten papers':",
          json.dumps(get_relevant_skill("extract sample sizes from these ten papers"))[:200])
    print("match for 'what's the capital of France':",
          json.dumps(get_relevant_skill("what's the capital of France"))[:200])
