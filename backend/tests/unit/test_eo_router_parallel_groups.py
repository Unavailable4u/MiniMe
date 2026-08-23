"""
tests/unit/test_eo_router_parallel_groups.py

Dedicated unit coverage for eo/router.py's sanitize_parallel_groups()
and its _flatten_role_names() helper.

Both were previously exercised only indirectly, through one broad
integration scenario in tests/integration/test_parallel_execution.py.
That's valuable end-to-end coverage, but it doesn't isolate each of
sanitize_parallel_groups()'s six independent safety checks (see that
function's own docstring in eo/router.py) -- a regression in any ONE
check could still pass the integration scenario if the others happen to
mask it. This file drives each check directly, plus the two branches
(pre-existing nested-group entries, and "nothing survived") that a
coverage run showed were never hit at all.

sanitize_parallel_groups() is the HARD gatekeeper for the parallel
execution feature: a bug that lets an unsafe candidate group through
means eo/executor.py's _run_concurrent_group() could run something it
was never staffed for, run something mid-way through a human-approval
checkpoint, or run the same role twice concurrently. Malformed/
adversarial input must always degrade to the flat, sequential order --
never raise, never silently do something unsafe -- so every test below
asserts the SAFE outcome, not just "no exception."
"""
from eo.router import sanitize_parallel_groups, _flatten_role_names, MAX_PARALLEL_GROUP_SIZE


def _hires(*roles):
    return [{"role": r, "agent_key": f"KEY_{r}", "brief": r} for r in roles]


# ---------------------------------------------------------------------------
# _flatten_role_names()
# ---------------------------------------------------------------------------

def test_flatten_role_names_collects_flat_string_entries():
    assert _flatten_role_names(["implementer", "verifier"]) == {"implementer", "verifier"}


def test_flatten_role_names_collects_members_of_nested_groups():
    assert _flatten_role_names([["writer_a", "writer_b"], "verifier"]) == {
        "writer_a", "writer_b", "verifier",
    }


def test_flatten_role_names_ignores_non_string_non_list_noise_and_empty_input():
    assert _flatten_role_names([]) == set()
    assert _flatten_role_names(None) == set()
    assert _flatten_role_names([123, None, "verifier", [456, "writer_a"]]) == {
        "verifier", "writer_a",
    }


# ---------------------------------------------------------------------------
# sanitize_parallel_groups() -- one test per independent safety check
# ---------------------------------------------------------------------------

def test_happy_path_two_member_group_is_collapsed_into_execution_order():
    hires = _hires("writer_a", "writer_b", "verifier")
    execution_order = ["writer_a", "writer_b", "verifier"]
    result = sanitize_parallel_groups(
        [["writer_a", "writer_b"]], execution_order, [], hires,
    )
    assert result == [["writer_a", "writer_b"], "verifier"]


def test_non_list_candidate_group_is_skipped():
    hires = _hires("writer_a", "writer_b")
    execution_order = ["writer_a", "writer_b"]
    result = sanitize_parallel_groups(
        ["writer_a", 42, "not_a_group"], execution_order, [], hires,
    )
    # None of the non-list "candidates" are valid groups -- unmodified.
    assert result == execution_order


def test_group_smaller_than_two_after_dedup_is_dropped():
    hires = _hires("writer_a")
    execution_order = ["writer_a"]
    # Duplicate entries collapse to a single member during within-group
    # dedup, which then fails the >= 2 members check.
    result = sanitize_parallel_groups(
        [["writer_a", "writer_a"]], execution_order, [], hires,
    )
    assert result == execution_order


def test_group_larger_than_max_size_is_dropped():
    roles = [f"writer_{i}" for i in range(MAX_PARALLEL_GROUP_SIZE + 1)]
    hires = _hires(*roles)
    execution_order = list(roles)
    result = sanitize_parallel_groups([roles], execution_order, [], hires)
    assert result == execution_order


def test_group_at_exactly_max_size_survives():
    roles = [f"writer_{i}" for i in range(MAX_PARALLEL_GROUP_SIZE)]
    hires = _hires(*roles)
    execution_order = list(roles)
    result = sanitize_parallel_groups([roles], execution_order, [], hires)
    assert result == [roles]


def test_group_member_never_actually_hired_is_dropped():
    # "writer_b" appears in the candidate group and in execution_order,
    # but was never staffed (not present in `hires`) -- a group can't
    # run something that was never hired.
    hires = _hires("writer_a")
    execution_order = ["writer_a", "writer_b"]
    result = sanitize_parallel_groups(
        [["writer_a", "writer_b"]], execution_order, [], hires,
    )
    assert result == execution_order


def test_group_member_absent_from_execution_order_is_dropped():
    # Both roles were hired, but "writer_b" isn't anywhere in
    # execution_order -- upstream disagrees with itself, so drop rather
    # than guess where the group belongs.
    hires = _hires("writer_a", "writer_b")
    execution_order = ["writer_a"]
    result = sanitize_parallel_groups(
        [["writer_a", "writer_b"]], execution_order, [], hires,
    )
    assert result == execution_order


def test_group_member_that_is_an_approval_checkpoint_is_dropped():
    # eo/executor.py's _run_concurrent_group() can't pause mid-group for
    # a human-approval checkpoint, so a checkpoint role can never be
    # folded into a concurrent group, even if everything else checks out.
    hires = _hires("writer_a", "approver")
    execution_order = ["writer_a", "approver"]
    result = sanitize_parallel_groups(
        [["writer_a", "approver"]], execution_order, ["approver"], hires,
    )
    assert result == execution_order


def test_second_group_overlapping_an_already_accepted_group_is_dropped_wholesale():
    # "writer_b" is claimed by the first (earlier) candidate group; the
    # second candidate reuses it alongside a brand-new, otherwise-valid
    # member ("writer_c"). The whole second candidate must be dropped --
    # not partially honored by keeping just "writer_c" -- since that
    # would silently produce a DIFFERENT group than anything proposed.
    hires = _hires("writer_a", "writer_b", "writer_c")
    execution_order = ["writer_a", "writer_b", "writer_c"]
    result = sanitize_parallel_groups(
        [["writer_a", "writer_b"], ["writer_b", "writer_c"]],
        execution_order, [], hires,
    )
    assert result == [["writer_a", "writer_b"], "writer_c"]


def test_nothing_survives_returns_execution_order_unmodified():
    # Every candidate fails at least one check -- the function must hand
    # back execution_order's contents completely unmodified (same
    # elements, same order), not some other reshuffled result.
    hires = _hires("writer_a")
    execution_order = ["writer_a", "verifier"]
    result = sanitize_parallel_groups(
        [["writer_a", "not_hired_role"]], execution_order, [], hires,
    )
    assert result == execution_order


def test_empty_parallel_groups_returns_execution_order_unmodified():
    hires = _hires("writer_a")
    execution_order = ["writer_a"]
    assert sanitize_parallel_groups([], execution_order, [], hires) == execution_order
    assert sanitize_parallel_groups(None, execution_order, [], hires) == execution_order


def test_preexisting_nested_group_untouched_by_this_pass_survives_with_no_candidates():
    # A workflow template can already carry a nested group in
    # execution_order from a PRIOR sanitize pass. If this pass's own
    # candidates don't touch any of its members, that pre-existing group
    # must be left exactly where it was, not flattened or re-validated.
    hires = _hires("writer_a", "writer_b", "verifier")
    execution_order = [["writer_a", "writer_b"], "verifier"]
    result = sanitize_parallel_groups([], execution_order, [], hires)
    assert result == execution_order
    assert result[0] == ["writer_a", "writer_b"]


def test_preexisting_nested_group_passes_through_alongside_a_newly_accepted_group():
    # Same pass-through guarantee, but this time at least one OTHER
    # candidate group is accepted elsewhere in execution_order -- so the
    # function goes through the full rebuild loop (not the early
    # "nothing survived" shortcut above), and the pre-existing nested
    # entry ([\"writer_a\", \"writer_b\"]) must still come out untouched,
    # exactly where it started, since none of its members are mentioned
    # in this pass's own accepted candidates.
    hires = _hires("writer_a", "writer_b", "writer_c", "writer_d")
    execution_order = [["writer_a", "writer_b"], "writer_c", "writer_d"]
    result = sanitize_parallel_groups(
        [["writer_c", "writer_d"]], execution_order, [], hires,
    )
    assert result == [["writer_a", "writer_b"], ["writer_c", "writer_d"]]


def test_within_group_duplicates_are_deduped_before_membership_checks():
    hires = _hires("writer_a", "writer_b")
    execution_order = ["writer_a", "writer_b"]
    result = sanitize_parallel_groups(
        [["writer_a", "writer_b", "writer_a"]], execution_order, [], hires,
    )
    assert result == [["writer_a", "writer_b"]]


def test_candidates_are_processed_in_given_order_first_wins_on_overlap():
    # Processing order matters for a deterministic winner among
    # overlapping candidates -- confirm the FIRST-listed candidate is
    # the one that gets to claim a contested role.
    hires = _hires("writer_a", "writer_b", "writer_c")
    execution_order = ["writer_a", "writer_b", "writer_c"]
    result_first_wins = sanitize_parallel_groups(
        [["writer_a", "writer_b"], ["writer_a", "writer_c"]],
        execution_order, [], hires,
    )
    assert result_first_wins == [["writer_a", "writer_b"], "writer_c"]
