"""Patch L01a: the model-facing catalog now holds ``ask_user`` and ``tasks``.

The server runs neither tool: it forwards them to the device like any other (S09's broker) and the person's
answer comes back as an ordinary ``tool_result``.  So the only thing the backend can check is the CATALOG: the
two definitions load, are R0 (visible in plan mode), validate model input the way the schema says and are
offered to the provider with their descriptions.  Nothing here touches a database or a network.
"""
import pytest

from tests.unit.loop_testkit import catalog

ASK = {"question": "Which database?", "options": [{"label": "SQLite"}, {"label": "Postgres", "description": "server"}]}
TASKS = {"tasks": [{"id": "t1", "title": "Read", "status": "done"},
                   {"id": "t2", "title": "Write", "status": "pending", "depends_on": ["t1"]}]}


def test_both_tools_are_in_the_catalog_as_r0():
    cat = catalog()
    assert {"ask_user", "tasks"} <= cat.names and len(cat.names) == 11
    assert cat.effective_risk("ask_user") == "R0" and cat.effective_risk("tasks") == "R0"


def test_both_are_offered_in_plan_mode_too():
    # neither writes or runs anything, so plan mode keeps them: the model may ask or keep a list while planning
    plan = {spec.name for spec in catalog().visible("plan")}
    assert plan == {"read_file", "list_dir", "glob", "grep", "plan_exit", "ask_user", "tasks"}
    assert {spec.name for spec in catalog().visible("ask")} == set(catalog().names)


def test_the_provider_sees_a_name_a_description_and_a_schema():
    specs = {spec.name: spec for spec in catalog().visible("ask")}
    for name in ("ask_user", "tasks"):
        spec = specs[name]
        assert spec.description and spec.input_schema["type"] == "object"
    assert "WHOLE list" in specs["tasks"].description
    assert "blocks" in specs["ask_user"].description or "wait" in specs["ask_user"].description


@pytest.mark.parametrize("name,args", [
    ("ask_user", {"question": "Which?"}),
    ("ask_user", ASK),
    ("ask_user", {"question": "Which?", "allow_free_text": False, "timeout_s": 3600, "options": [{"label": "A"}]}),
    ("tasks", {"tasks": []}),
    ("tasks", TASKS),
])
def test_good_input_validates(name, args):
    assert catalog().validate(name, args) == []


@pytest.mark.parametrize("name,args,needle", [
    ("ask_user", {}, "question"),
    ("ask_user", {"question": ""}, "minLength"),
    ("ask_user", {"question": "x" * 2001}, "maxLength"),
    ("ask_user", {"question": 5}, "type"),
    ("ask_user", {"question": "q", "options": [{"label": f"o{i}"} for i in range(7)]}, "maxItems"),
    ("ask_user", {"question": "q", "options": [{"description": "d"}]}, "label"),
    ("ask_user", {"question": "q", "options": [{"label": "A", "value": 1}]}, "unexpected"),
    ("ask_user", {"question": "q", "timeout_s": 0}, "minimum"),
    ("ask_user", {"question": "q", "timeout_s": 3601}, "maximum"),
    ("ask_user", {"question": "q", "colour": "red"}, "unexpected"),
    ("tasks", {}, "tasks"),
    ("tasks", {"tasks": [{"id": "t1", "title": "x", "status": "stuck"}]}, "enum"),
    ("tasks", {"tasks": [{"id": "t1", "status": "done"}]}, "title"),
    ("tasks", {"tasks": [{"id": "t%d" % i, "title": "x", "status": "pending"} for i in range(31)]}, "maxItems"),
    ("tasks", {"tasks": [], "extra": 1}, "unexpected"),
])
def test_bad_input_is_named_without_repeating_the_model_text(name, args, needle):
    problems = catalog().validate(name, args)
    assert problems and any(needle in p for p in problems), problems


def test_a_problem_never_contains_the_models_value():
    marker = "zq-canary-" + "a41c07"
    problems = catalog().validate("tasks", {"tasks": [{"id": marker, "title": marker, "status": marker}]})
    assert problems and all(marker not in p for p in problems)
