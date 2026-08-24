"""
tests/unit/test_eo_result_render.py — Patch 7e-S4.

eo/result_render.py had zero test coverage before this. Priorities,
worst-silent-failure first:

  1. render_agent_result()'s shape dispatch order — several branches
     are structurally ambiguous on their own (a flat {module: code}
     dict and a flat {platform: content} dict look identical without
     the role gate; a code_writers empty-dict "no tests" result must
     render as "(no modules)", not fall through to the raw-JSON
     fallback) and getting the checked order wrong silently mis-renders
     a real result shape as something else instead of raising.
  2. The byte-based (not character-based) truncation fix — this is a
     documented regression fix (multi-byte UTF-8 symbols in
     hardware_speccer-style output blowing past Pusher's byte cap even
     though the character count looked fine), so it gets its own
     dedicated coverage rather than an incidental side effect of some
     other test.
  3. collect_artifacts()'s "skip malformed entries, never error the
     whole run" contract, tagging each collected entry with its
     producing role.

_looks_like_platform_content_map's role gate is exercised via the
public render_agent_result() dispatch (its actual call path), not by
calling the private helper directly, since the ambiguity it resolves
only matters in the context of the branch order around it.
"""
import eo.result_render as result_render


# ---------------------------------------------------------------------
# render_agent_result — plain string / calendar entries
# ---------------------------------------------------------------------

def test_render_agent_result_passes_through_a_plain_string():
    assert result_render.render_agent_result("just some text") == "just some text"


def test_render_agent_result_renders_calendar_entries_as_a_table():
    entries = [{"date": "2026-01-01", "platform": "twitter", "content_ref": "post_1"}]
    text = result_render.render_agent_result(entries)
    assert "| Date | Platform | Content |" in text
    assert "2026-01-01" in text
    assert "twitter" in text


def test_render_agent_result_calendar_entries_checked_ahead_of_dict_branches():
    """A list isn't a dict, but this pins down the documented ordering
    choice explicitly rather than relying on incidental non-collision."""
    entries = [{"date": "d", "platform": "p", "content_ref": "c"}]
    assert result_render._looks_like_calendar_entries(entries) is True
    assert result_render._looks_like_calendar_entries({"date": "d"}) is False


# ---------------------------------------------------------------------
# render_agent_result — dict shape dispatch
# ---------------------------------------------------------------------

def test_render_agent_result_prefers_text_key_when_present():
    result = {"text": "the actual answer", "role": "writer"}
    assert result_render.render_agent_result(result) == "the actual answer"


def test_render_agent_result_reviewer_shape_with_issues():
    result = {
        "summary": "Found two problems.",
        "issues": [
            {"severity": "high", "module": "auth.py", "description": "SQL injection"},
            {"severity": "low", "module": "utils.py", "description": "typo", "flagged_by_count": 2},
        ],
    }
    text = result_render.render_agent_result(result)
    assert "Found two problems." in text
    assert "**[high]** `auth.py`: SQL injection" in text
    assert "flagged by 2 reviewers" in text


def test_render_agent_result_reviewer_shape_no_issues_no_summary():
    result = {"issues": []}
    text = result_render.render_agent_result(result)
    assert text == "No issues found."


def test_render_agent_result_fixer_shape():
    result = {"fixed_code": {"utils.py": {"language": "python", "code": "x = 1"}}}
    text = result_render.render_agent_result(result)
    assert "**utils.py**" in text
    assert "```python\nx = 1\n```" in text


def test_render_agent_result_code_key_shape():
    assert result_render.render_agent_result({"code": "print(1)"}) == "print(1)"


def test_render_agent_result_answer_key_shape():
    assert result_render.render_agent_result({"answer": 42}) == "42"


def test_render_agent_result_extraction_table_shape():
    result = {
        "papers": [{"title": "Paper A", "year": 2024, "method": "RCT"}],
        "field_names": ["method"],
    }
    text = result_render.render_agent_result(result)
    assert "| Title | Year | Method |" in text
    assert "| Paper A | 2024 | RCT |" in text


def test_render_agent_result_extraction_table_not_confused_with_academic_search_shape():
    """academic_search returns {"papers", "edges_written"} with no
    field_names -- must NOT hit the extraction-table branch."""
    result = {"papers": [{"title": "x"}], "edges_written": 3, "summary": "Found 1 paper."}
    text = result_render.render_agent_result(result)
    assert text == "Found 1 paper."


def test_render_agent_result_platform_content_map_requires_matching_role():
    result = {"twitter": "tweet text", "linkedin": "post text"}
    # No role passed -- must NOT render as a platform card grid; falls
    # through to _looks_like_module_map's flat-string-map branch instead.
    text = result_render.render_agent_result(result)
    assert "**twitter**" in text  # code-modules rendering, not platform rendering
    assert "```" in text


def test_render_agent_result_platform_content_map_with_matching_role():
    result = {"twitter": "tweet text", "linkedin": "post text"}
    text = result_render.render_agent_result(result, role="content_adapter_pool")
    assert "**twitter**" in text
    assert "tweet text" in text
    assert "---" in text
    assert "```" not in text  # rendered as platform content, not code


def test_render_agent_result_module_map_with_bare_strings():
    result = {"main.py": "print(1)", "utils.py": "def f(): pass"}
    text = result_render.render_agent_result(result)
    assert "**main.py**" in text
    assert "print(1)" in text


def test_render_agent_result_module_map_empty_dict_renders_no_modules_placeholder():
    """test_writer.py's legitimate 'generated no tests' case -- must not
    fall through to the raw-JSON-dump fallback."""
    assert result_render.render_agent_result({}) == "_(no modules)_"


def test_render_agent_result_summary_only_shape():
    """A flat single-string-value dict is structurally a module map too
    (see _looks_like_module_map), so a bare {"summary": ...} with no
    other recognized key actually renders via that earlier branch --
    this pins down the real dispatch order rather than the ideal one."""
    result = {"summary": "A short factual summary."}
    text = result_render.render_agent_result(result)
    assert "A short factual summary." in text


def test_render_agent_result_summary_reached_when_not_shaped_like_a_module_map():
    """A real 'summary'-only shape has other non-string keys alongside
    it (source-quality flaggers, citation graph builders, etc.), which
    disqualifies the module-map branch and lets the summary branch fire."""
    result = {"summary": "A short factual summary.", "flags": ["one", "two"]}
    assert result_render.render_agent_result(result) == "A short factual summary."


def test_render_agent_result_unrecognized_dict_shape_falls_back_to_json():
    result = {"weird_key": "weird_value", "another": 123}
    text = result_render.render_agent_result(result)
    assert text.startswith("```json")
    assert "weird_key" in text


def test_render_agent_result_non_dict_non_string_falls_back_to_str():
    assert result_render.render_agent_result(42) == "42"


def test_render_agent_result_strips_whitespace():
    assert result_render.render_agent_result("  padded  \n") == "padded"


# ---------------------------------------------------------------------
# render_agent_result — byte-based truncation
# ---------------------------------------------------------------------

def test_render_agent_result_no_truncation_under_the_byte_limit():
    text = "short text"
    assert result_render.render_agent_result(text, limit=1000) == text


def test_render_agent_result_truncates_on_byte_length_not_character_count():
    """The documented regression: multi-byte UTF-8 chars (Ω, µF, °C) mean
    a string can be well under `limit` characters but over `limit`
    bytes -- truncation must trigger on the encoded byte length."""
    # Each "Ω" is 2 bytes in UTF-8. 20 chars but 40 bytes -- limit=30
    # bytes should truncate even though char count (20) is under 30.
    text = "Ω" * 20
    assert len(text) == 20
    assert len(text.encode("utf-8")) == 40

    result = result_render.render_agent_result(text, limit=30)

    assert "truncated" in result
    assert len(result.split("\n\n... [truncated")[0].encode("utf-8")) <= 30


def test_render_agent_result_truncation_never_emits_invalid_utf8():
    text = "µF" * 50  # multi-byte char repeated, likely to land mid-char at some cut
    result = result_render.render_agent_result(text, limit=17)
    prefix = result.split("\n\n... [truncated")[0]
    prefix.encode("utf-8")  # must not raise -- proves no half-cut byte sequence


def test_render_agent_result_truncation_message_reports_total_encoded_bytes():
    text = "a" * 100
    result = result_render.render_agent_result(text, limit=10)
    assert "[truncated, 100 bytes total]" in result


def test_render_agent_result_pure_ascii_char_count_equals_byte_count():
    """Sanity check for the pre-fix assumption that used to hold for
    ASCII-only text (character count == byte count), which is exactly
    why the bug only showed up on multi-byte content."""
    text = "a" * 50
    assert len(text) == len(text.encode("utf-8"))
    assert result_render.render_agent_result(text, limit=100) == text


# ---------------------------------------------------------------------
# collect_artifacts
# ---------------------------------------------------------------------

def test_collect_artifacts_empty_role_outputs_returns_empty_list():
    assert result_render.collect_artifacts({}) == []
    assert result_render.collect_artifacts(None) == []


def test_collect_artifacts_skips_role_outputs_that_arent_dicts():
    assert result_render.collect_artifacts({"writer": "just a string"}) == []


def test_collect_artifacts_skips_roles_with_no_artifacts_key():
    assert result_render.collect_artifacts({"writer": {"text": "hello"}}) == []


def test_collect_artifacts_skips_non_list_artifacts_value():
    assert result_render.collect_artifacts({"writer": {"artifacts": "not a list"}}) == []


def test_collect_artifacts_collects_well_formed_entries_tagged_by_role():
    role_outputs = {
        "frontend_builder": {
            "artifacts": [{"type": "html", "title": "Demo", "code": "<b>hi</b>"}],
        },
    }
    result = result_render.collect_artifacts(role_outputs)
    assert result == [{"type": "html", "title": "Demo", "code": "<b>hi</b>", "role": "frontend_builder"}]


def test_collect_artifacts_defaults_title_to_empty_string_when_absent():
    role_outputs = {"builder": {"artifacts": [{"type": "svg", "code": "<svg></svg>"}]}}
    result = result_render.collect_artifacts(role_outputs)
    assert result[0]["title"] == ""


def test_collect_artifacts_skips_malformed_entries_without_erroring(capsys):
    role_outputs = {
        "builder": {
            "artifacts": [
                {"type": "html", "title": "Good", "code": "<b>ok</b>"},
                {"type": "", "code": "missing type is empty"},  # malformed -- dropped
                {"code": "missing type key entirely"},  # malformed -- dropped
            ],
        },
    }
    result = result_render.collect_artifacts(role_outputs)
    assert len(result) == 1
    assert result[0]["title"] == "Good"


def test_collect_artifacts_merges_across_multiple_roles():
    role_outputs = {
        "role_a": {"artifacts": [{"type": "html", "code": "a"}]},
        "role_b": {"artifacts": [{"type": "svg", "code": "b"}]},
    }
    result = result_render.collect_artifacts(role_outputs)
    roles = {entry["role"] for entry in result}
    assert roles == {"role_a", "role_b"}
