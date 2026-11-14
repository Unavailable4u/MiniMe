"""
m2c/tests/interact/test_interact_schema_match.py -- Patch L01a: the limits in ``m2c.interact`` are the schemas' limits.

The constants in question.py and tasks.py are a hand-written copy of what ``protocol/v1/tools/ask_user.json`` and
``tasks.json`` say (the client's synced copy), so a schema edit that is not followed here fails this test instead of
letting the two disagree.  Every input the schema accepts is also accepted by the parsers, and the other way round
for the rules a schema can express.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import m2c
from m2c.interact import question as q
from m2c.interact import tasks as t

TOOLS = Path(m2c.__file__).resolve().parent / "protocol" / "v1" / "tools"


def schema(name):
    return json.loads((TOOLS / f"{name}.json").read_text(encoding="utf-8"))


def test_ask_user_limits_match_the_schema():
    d = schema("ask_user")
    props = d["input_schema"]["properties"]
    option = props["options"]["items"]["properties"]
    assert d["risk"] == "R0" and "risk_max" not in d
    assert props["question"]["maxLength"] == q.MAX_QUESTION_CHARS and props["question"]["minLength"] == 1
    assert props["options"]["maxItems"] == q.MAX_OPTIONS
    assert option["label"]["maxLength"] == q.MAX_LABEL_CHARS
    assert option["description"]["maxLength"] == q.MAX_OPTION_DESCRIPTION_CHARS
    assert props["options"]["items"]["required"] == ["label"]
    assert props["allow_free_text"]["default"] is True
    assert props["timeout_s"]["default"] == q.DEFAULT_TIMEOUT_S
    assert (props["timeout_s"]["minimum"], props["timeout_s"]["maximum"]) == (q.MIN_TIMEOUT_S, q.MAX_TIMEOUT_S)
    assert set(props) == {"question", "options", "allow_free_text", "timeout_s"} and d["input_schema"]["required"] == ["question"]


def test_tasks_limits_match_the_schema():
    d = schema("tasks")
    item = d["input_schema"]["properties"]["tasks"]["items"]
    props = item["properties"]
    assert d["risk"] == "R0" and "risk_max" not in d
    assert d["input_schema"]["properties"]["tasks"]["maxItems"] == t.MAX_TASKS
    assert props["id"]["maxLength"] == t.MAX_ID_CHARS and props["title"]["maxLength"] == t.MAX_TITLE_CHARS
    assert tuple(props["status"]["enum"]) == t.STATUSES
    assert props["depends_on"]["maxItems"] == t.MAX_TASKS - 1 and props["depends_on"]["items"]["maxLength"] == t.MAX_ID_CHARS
    assert set(item["required"]) == {"id", "title", "status"} and set(props) == {"id", "title", "status", "depends_on"}


@pytest.mark.parametrize("args", [
    {"question": "Which?"},
    {"question": "Which?", "options": [{"label": "A"}, {"label": "B", "description": "second"}], "allow_free_text": False,
     "timeout_s": 5},
])
def test_what_the_schema_accepts_the_parser_accepts(args):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(args, schema("ask_user")["input_schema"])
    q.parse_question(args)


def test_the_synced_copy_is_the_source():
    source = Path(__file__).resolve().parents[3] / "protocol" / "v1" / "tools"
    if source.is_dir():                                    # absent when the tests run from an installed wheel
        for name in ("ask_user", "tasks"):
            assert (source / f"{name}.json").read_bytes() == (TOOLS / f"{name}.json").read_bytes()
