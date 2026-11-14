"""
m2c/tests/interact/test_interact_tasks.py -- Patch L01a: the ``tasks`` list, its validation and its sink interface.

The model sends the WHOLE list on every call.  ``parse_tasks`` turns it into a tuple of frozen ``Task`` values or
fails closed with a FIXED message naming the rule that failed (it never repeats an id or a title).
"""
from __future__ import annotations

import dataclasses

import pytest

from m2c.interact import tasks as t
from m2c.interact.tasks import Task, TaskListError, parse_tasks, summarize

CANARY = "zq-canary-" + "51d0e8"


def task(id_, title="do it", status="pending", **extra):
    return {"id": id_, "title": title, "status": status, **extra}


def rule_of(raw):
    with pytest.raises(TaskListError) as err:
        parse_tasks(raw)
    return err.value.message


# ---- good lists --------------------------------------------------------------------------------------------

def test_a_list_becomes_frozen_tasks_in_order():
    parsed = parse_tasks([task("t1", "Read the code", "done"),
                          task("t2", "Write tests", "in_progress", depends_on=["t1"]),
                          task("t3", "Ship", "blocked", depends_on=["t1", "t2"])])
    assert parsed == (Task("t1", "Read the code", "done", ()),
                      Task("t2", "Write tests", "in_progress", ("t1",)),
                      Task("t3", "Ship", "blocked", ("t1", "t2")))
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed[0].status = "pending"                                            # type: ignore[misc]


def test_an_empty_list_is_allowed_and_clears_the_list():
    assert parse_tasks([]) == ()


def test_every_status_is_accepted():
    assert t.STATUSES == ("pending", "in_progress", "done", "blocked")
    for status in t.STATUSES:
        assert parse_tasks([task("a", status=status)])[0].status == status


def test_thirty_tasks_are_the_most_and_a_dependency_chain_is_fine():
    ok = [task(f"t{i}", depends_on=[f"t{i - 1}"] if i else []) for i in range(30)]
    assert len(parse_tasks(ok)) == 30
    assert t.MAX_TASKS == 30


def test_a_diamond_is_not_a_cycle():
    parsed = parse_tasks([task("a"), task("b", depends_on=["a"]), task("c", depends_on=["a"]),
                          task("d", depends_on=["b", "c"])])
    assert [x.id for x in parsed] == ["a", "b", "c", "d"]


def test_dependencies_may_name_a_task_that_comes_later_in_the_list():
    parsed = parse_tasks([task("b", depends_on=["a"]), task("a")])
    assert parsed[0].depends_on == ("a",)


def test_the_length_limits_are_inclusive():
    parse_tasks([task("i" * 64, "T" * 200)])
    parse_tasks([task("a")] + [task(f"d{i}", depends_on=["a"]) for i in range(5)])


def test_as_dict_is_the_wire_shape():
    assert Task("t1", "Go", "pending", ("t0",)).as_dict() == {
        "id": "t1", "title": "Go", "status": "pending", "depends_on": ["t0"]}
    assert Task("t1", "Go", "done").as_dict() == {"id": "t1", "title": "Go", "status": "done", "depends_on": []}


# ---- the rules, each with its own fixed message --------------------------------------------------------------

def test_more_than_thirty_tasks_is_refused():
    assert rule_of([task(f"t{i}") for i in range(31)]) == t.TOO_MANY


def test_duplicate_ids_are_refused():
    assert rule_of([task("a"), task("b"), task("a", "again")]) == t.DUPLICATE_IDS


def test_an_unknown_dependency_is_refused():
    assert rule_of([task("a", depends_on=["ghost"])]) == t.UNKNOWN_DEPENDENCY


def test_a_cycle_is_refused():
    assert rule_of([task("a", depends_on=["b"]), task("b", depends_on=["a"])]) == t.CYCLE


def test_a_longer_cycle_is_refused():
    assert rule_of([task("a", depends_on=["c"]), task("b", depends_on=["a"]), task("c", depends_on=["b"]),
                    task("d")]) == t.CYCLE


def test_a_task_that_depends_on_itself_is_a_cycle():
    assert rule_of([task("a", depends_on=["a"])]) == t.CYCLE


@pytest.mark.parametrize("status", ["", "stuck", "DONE", "Pending", "in progress", None, 3, True, ["done"]])
def test_a_bad_status_is_refused(status):
    assert rule_of([task("a", status=status)]) == t.BAD_STATUS


def test_a_missing_status_is_refused():
    assert rule_of([{"id": "a", "title": "x"}]) == t.BAD_ITEM


@pytest.mark.parametrize("raw", [None, "t1", 5, {"id": "a"}, ("a",)])
def test_the_list_must_be_an_array(raw):
    assert rule_of(raw) == t.NOT_A_LIST


@pytest.mark.parametrize("item", ["t1", 5, None, [], {"id": "a", "title": "x"}, {"title": "x", "status": "done"},
                                  {"id": "a", "status": "done"}, task("a", owner="me")])
def test_an_item_must_be_an_object_with_exactly_the_known_keys(item):
    assert rule_of([item]) == t.BAD_ITEM


@pytest.mark.parametrize("bad_id", ["", " ", "a\nb", "a\x1bb", 5, None, "i" * 65])
def test_a_bad_id_is_refused(bad_id):
    assert rule_of([task(bad_id)]) == t.BAD_ID


@pytest.mark.parametrize("bad_title", ["", "   ", "a\nb", "a\x1b[2Jb", 5, None, "T" * 201])
def test_a_bad_title_is_refused(bad_title):
    assert rule_of([task("a", bad_title)]) == t.BAD_TITLE


@pytest.mark.parametrize("deps", ["a", 5, [""], [5], ["b", "b"], ["x" * 65], [None], {"a": 1},
                                  [f"d{i}" for i in range(30)]])
def test_bad_depends_on_is_refused(deps):
    assert rule_of([task("a", depends_on=deps), task("b")]) == t.BAD_DEPENDS_ON


def test_depends_on_may_be_left_out_or_empty():
    assert parse_tasks([task("a"), task("b", depends_on=[])]) == (Task("a", "do it", "pending", ()),
                                                                  Task("b", "do it", "pending", ()))


def test_the_first_failing_rule_is_the_one_reported_in_a_fixed_order():
    # structure before ids, ids before dependencies, dependencies before cycles
    assert rule_of([task("a", status="x"), task("a")]) == t.BAD_STATUS
    assert rule_of([task("a"), task("a", depends_on=["ghost"])]) == t.DUPLICATE_IDS
    assert rule_of([task("a", depends_on=["ghost", "b"]), task("b", depends_on=["a"])]) == t.UNKNOWN_DEPENDENCY


def test_messages_name_the_rule_and_never_repeat_the_models_text():
    for raw in (
        [task(CANARY, depends_on=[CANARY + "-ghost"])],
        [task(CANARY), task(CANARY)],
        [task("a", CANARY + "\x1b")],
        [task("a", status=CANARY)],
        [task("a", **{CANARY: 1})],
        [task("a", depends_on=[CANARY, CANARY])],
        [task(CANARY, depends_on=[CANARY])],
        CANARY,
    ):
        message = rule_of(raw)
        assert CANARY not in message
        assert message.startswith("error: invalid task list: ") and "\n" not in message


def test_each_rule_has_its_own_message():
    messages = {t.NOT_A_LIST, t.TOO_MANY, t.BAD_ITEM, t.BAD_ID, t.BAD_TITLE, t.BAD_STATUS, t.BAD_DEPENDS_ON,
                t.DUPLICATE_IDS, t.UNKNOWN_DEPENDENCY, t.CYCLE}
    assert len(messages) == 10 and all(m.startswith("error: invalid task list: ") for m in messages)


# ---- the confirmation the model reads ----------------------------------------------------------------------

def test_the_confirmation_has_counts():
    parsed = parse_tasks([task("a", status="done"), task("b", status="done"), task("c", status="in_progress"),
                          task("d"), task("e", status="blocked")])
    assert summarize(parsed) == "Task list updated: 5 tasks (1 pending, 1 in progress, 2 done, 1 blocked)."


def test_the_confirmation_for_one_task_and_for_none():
    assert summarize(parse_tasks([task("a")])) == "Task list updated: 1 task (1 pending, 0 in progress, 0 done, 0 blocked)."
    assert summarize(()) == "Task list cleared."


def test_the_confirmation_never_contains_a_title():
    assert CANARY not in summarize(parse_tasks([task("a", CANARY)]))


# ---- the sink interface ------------------------------------------------------------------------------------

def test_a_plain_function_satisfies_the_sink_interface():
    seen = []

    def sink(tasks: t.TaskList) -> None:
        seen.append(tasks)

    sink(parse_tasks([task("a")]))
    assert seen == [(Task("a", "do it", "pending", ()),)] and t.EVENT_TASKS == "tasks"
