"""
tests/unit/test_resolve_role.py — rebuilt around the current
eo/registry.py, whose resolve_role() no longer raises KeyError for an
unmapped role (Migration Part 10 §2.1 replaced the old ROLE_TO_AGENT /
KeyError design entirely). Every real-action role listed in
REAL_ACTION_ROLES resolves to its dedicated module name; every other
role name -- known or brand new -- resolves to the literal string
"generic_worker".

Robustness-fix section added below: the two extra resolution layers
(ROLE_ALIASES exact match, then a conservative difflib fuzzy match --
both documented directly above ROLE_ALIASES in eo/registry.py) were
completely untested before this file. Both matter for the same reason
the plain REAL_ACTION_ROLES lookup does: a miss here doesn't error, it
silently reroutes a task that needed a dedicated module (writes a real
BOM / real Mermaid diagram) into generic_worker's free-text reasoning
instead -- no exception, no log a caller would notice, just a task that
quietly never produces the artifact it was supposed to.
"""
from eo.registry import (
    REAL_ACTION_ROLES,
    ROLE_ALIASES,
    _fuzzy_resolve_specialized_role,
    _normalize_role_label,
    resolve_role,
)


def test_unmapped_role_falls_back_to_generic_worker():
    unmapped_role = "some_role_nobody_ever_mapped_xyz"
    assert unmapped_role not in REAL_ACTION_ROLES, (
        "test role accidentally collides with a real mapping"
    )
    assert resolve_role(unmapped_role) == "generic_worker"


def test_real_action_role_resolves_to_its_dedicated_module():
    assert resolve_role("implementer") == "code_writers"
    assert resolve_role("verifier") == "reviewer"
    assert resolve_role("fixer") == "fixer_pool"


def test_every_real_action_role_resolves_to_its_own_mapping():
    for role_name, expected_module in REAL_ACTION_ROLES.items():
        assert resolve_role(role_name) == expected_module


def test_retired_dedicated_modules_now_fall_back_to_generic_worker():
    # Migration Part 27: changelog_writer, final_qa, and gatekeeper's
    # dedicated agent modules were retired. Their role names are still
    # valid for the Panel to hire, but -- not being in
    # REAL_ACTION_ROLES -- they now resolve straight to generic_worker
    # instead of a module that no longer exists.
    for role_name in ("changelog_writer", "final_qa", "gatekeeper"):
        assert role_name not in REAL_ACTION_ROLES
        assert resolve_role(role_name) == "generic_worker"


# ---------------------------------------------------------------------------
# ROLE_ALIASES exact-match layer (resolution order step 2)
# ---------------------------------------------------------------------------

def test_curated_alias_exact_match_resolves_to_canonical_real_action_role():
    # "hardware_designer" is a curated synonym for "hardware_speccer" in
    # ROLE_ALIASES; a classifier that emits the synonym instead of the
    # canonical role name must still land on the real dedicated module,
    # not fall through to generic_worker.
    assert "hardware_speccer" in ROLE_ALIASES
    assert "hardware_designer" in ROLE_ALIASES["hardware_speccer"]
    assert resolve_role("hardware_designer") == REAL_ACTION_ROLES["hardware_speccer"]


def test_alias_match_is_case_and_separator_insensitive():
    # _normalize_role_label() lowercases and folds "-"/" " to "_" before
    # lookup, so a differently-cased or hyphenated classifier label for
    # the same synonym still resolves.
    assert resolve_role("Hardware-Designer") == REAL_ACTION_ROLES["hardware_speccer"]
    assert resolve_role("HARDWARE DESIGNER") == REAL_ACTION_ROLES["hardware_speccer"]


def test_alias_canonical_name_itself_resolves_via_the_lookup_too():
    # _build_alias_lookup() maps each canonical name to itself as well as
    # every synonym, so the canonical spelling resolves the same way.
    assert resolve_role("architecture_diagrammer") == REAL_ACTION_ROLES["architecture_diagrammer"]


def test_every_curated_alias_resolves_to_its_canonical_real_action_role():
    for canonical, synonyms in ROLE_ALIASES.items():
        assert canonical in REAL_ACTION_ROLES, (
            f"ROLE_ALIASES canonical '{canonical}' isn't in REAL_ACTION_ROLES"
        )
        for synonym in synonyms:
            assert resolve_role(synonym) == REAL_ACTION_ROLES[canonical], (
                f"alias '{synonym}' -> expected '{REAL_ACTION_ROLES[canonical]}'"
            )


def test_cad_designer_resolves_exactly_to_hardware_speccer():
    # Patch L.1: before this alias existed, "cad_designer" only ever
    # cleared resolve_role() via the fuzzy fallback (a log line +
    # cutoff=0.82 guess), not an exact match. This asserts the specific,
    # confirmed-correct target the guide's own log suggestion (verbatim:
    # schema_diagrammer) was checked against and rejected -- schema_diagrammer
    # is an ER/database-diagram role, semantically wrong for a
    # 3D-mechanical-model request; hardware_speccer is the module that
    # actually produces mech/CAD-adjacent output (workspace_facts.custom["mech"]).
    assert "cad_designer" in ROLE_ALIASES["hardware_speccer"]
    assert resolve_role("cad_designer") == REAL_ACTION_ROLES["hardware_speccer"]
    assert resolve_role("cad_designer") != REAL_ACTION_ROLES["schema_diagrammer"]


def test_cad_designer_no_longer_needs_the_fuzzy_fallback():
    # Confirms the specific regression this patch closes: "cad_designer"
    # now resolves via the exact ROLE_ALIASES lookup (resolve_role()'s
    # step 2), so the fuzzy helper (step 3) is never even reached for
    # it -- no `cutoff=0.82` log line, no fuzzy-match uncertainty.
    # Verified directly against resolve_role()'s own resolution order:
    # the normalized label must already be an exact key in the alias
    # lookup table step 2 checks, which is what makes step 3
    # unreachable for this input.
    from eo.registry import _ALIAS_LOOKUP, _normalize_role_label
    assert _normalize_role_label("cad_designer") in _ALIAS_LOOKUP
    assert _ALIAS_LOOKUP[_normalize_role_label("cad_designer")] == "hardware_speccer"


def test_cad_designer_resolution_prints_no_fuzzy_match_log_line(capsys):
    # _fuzzy_resolve_specialized_role() prints its own log line
    # ("fuzzy-matched '...' -> '...' ... Consider adding ...") only when
    # IT is actually invoked and finds a match. Since resolve_role()
    # short-circuits at the exact-alias step for "cad_designer" now,
    # that print must never fire for this input -- the literal "no
    # fuzzy-match log line" half of this patch's own "Done when".
    resolve_role("cad_designer")
    captured = capsys.readouterr()
    assert "fuzzy-matched" not in captured.out


# ---------------------------------------------------------------------------
# Conservative fuzzy-match fallback (resolution order step 3)
# ---------------------------------------------------------------------------

def test_close_typo_of_a_curated_alias_fuzzy_matches_to_the_real_module():
    # One dropped/extra character from a real curated synonym should
    # still clear the high (0.82) cutoff and resolve to the same real
    # module the exact synonym would.
    typo = "hardware_designerr"
    assert typo not in ROLE_ALIASES.get("hardware_speccer", [])
    assert resolve_role(typo) == REAL_ACTION_ROLES["hardware_speccer"]


def test_fuzzy_resolve_helper_returns_none_below_cutoff():
    # A label with no meaningfully close curated alias must not match --
    # this is the "stay conservative, let generic_worker take it" half
    # of the design (a false positive here silently misroutes a task).
    assert _fuzzy_resolve_specialized_role("banana_bread_recipe") is None
    assert resolve_role("banana_bread_recipe") == "generic_worker"


def test_fuzzy_resolve_helper_only_ever_returns_a_canonical_name():
    typo = "system_architec"  # close to the "system_architect" synonym
    canonical = _fuzzy_resolve_specialized_role(typo)
    assert canonical == "architecture_diagrammer"
    assert canonical in ROLE_ALIASES  # helper always returns a canonical key


def test_plain_real_action_role_with_no_alias_entry_is_never_a_fuzzy_candidate():
    # Roles like "code_writers"/"file_manager" deliberately have no
    # ROLE_ALIASES synonym list -- resolve_role() must still resolve the
    # exact name via REAL_ACTION_ROLES (step 1) without ever touching
    # the fuzzy pool, and a near-miss of one of THESE names must not be
    # invented as a fuzzy match either (the alias pool only contains the
    # three curated canonicals above).
    assert "code_writers" not in ROLE_ALIASES
    # "code_writers" is a resolved MODULE name, not a role name -- the
    # role name that maps to it is "implementer" (REAL_ACTION_ROLES).
    # Resolving the module name itself isn't in REAL_ACTION_ROLES, so it
    # correctly falls through to generic_worker, same as any other
    # role name nobody's mapped.
    assert resolve_role("implementer") == "code_writers"
    assert resolve_role("code_writers") == "generic_worker"
    assert _fuzzy_resolve_specialized_role("code_writerz") is None


def test_normalize_role_label_folds_case_hyphens_and_spaces():
    assert _normalize_role_label("Hardware-Designer") == "hardware_designer"
    assert _normalize_role_label("  DB Schema Designer  ") == "db_schema_designer"
