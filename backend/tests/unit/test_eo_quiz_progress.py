"""
tests/unit/test_eo_quiz_progress.py — Patch 7e-S5.

eo/quiz_progress.py had zero test coverage before this. Priorities,
worst-silent-failure first:

  1. parse_quiz()'s Markdown grammar, especially the BUGFIX'd option
     regex (bug #3 -- must accept both '-'/'*' bullets and loose
     whitespace, matching QuizRunner.jsx's own DISPLAY regex) and the
     "malformed question skipped, not raised" contract (no options, no
     marked answer, or more than one marked answer).
  2. grade_quiz()'s answer-index matching, including the documented
     "missing/short answers list == unanswered, not an error" posture.
  3. record_attempt()/list_attempts()/get_missed_questions()'s store
     semantics -- especially get_missed_questions()' s "reduce across
     attempts oldest->newest, keep only each question's LATEST result"
     behavior, which is the one place a bug here would show a person a
     stale "you got this wrong" after they'd since gotten it right.

Isolation follows test_eo_node_summaries.py's convention: this module
reads/writes a real JSON file on disk (PROGRESS_PATH), so tests
monkeypatch that path to a location under tmp_path. parse_quiz() and
grade_quiz() are pure functions over real Markdown text and are
exercised directly (through the real agents.importer.parse_markdown_text()
dependency, not mocked) rather than through the store.
"""

from eo import quiz_progress


def _use_tmp_path(monkeypatch, tmp_path):
    monkeypatch.setattr(quiz_progress, "PROGRESS_PATH", str(tmp_path / "_quiz_progress.json"))


_SIMPLE_QUIZ = (
    "# Demo Quiz\n\n"
    "## Q1: What color is the sky?\n\n"
    "- [ ] Green\n"
    "- [x] Blue\n"
    "- [ ] Red\n\n"
    "Explanation: Rayleigh scattering favors blue wavelengths.\n\n"
    "## Q2: What is 2 + 2?\n\n"
    "- [ ] 3\n"
    "- [x] 4\n"
    "- [ ] 5\n\n"
    "Explanation: Basic arithmetic.\n"
)


# ---------------------------------------------------------------------
# parse_quiz — grammar
# ---------------------------------------------------------------------

def test_parse_quiz_extracts_title_and_questions():
    result = quiz_progress.parse_quiz(_SIMPLE_QUIZ)
    assert result["title"] == "Demo Quiz"
    assert len(result["questions"]) == 2
    assert result["questions"][0]["question"] == "Q1: What color is the sky?"


def test_parse_quiz_marks_correct_index_from_x_checkbox():
    result = quiz_progress.parse_quiz(_SIMPLE_QUIZ)
    q1 = result["questions"][0]
    assert q1["options"] == ["Green", "Blue", "Red"]
    assert q1["correct_index"] == 1


def test_parse_quiz_extracts_explanation():
    result = quiz_progress.parse_quiz(_SIMPLE_QUIZ)
    assert "Rayleigh scattering" in result["questions"][0]["explanation"]


def test_parse_quiz_accepts_asterisk_bullets_bugfix_3():
    quiz = (
        "# Quiz\n\n"
        "## Q1: Test?\n\n"
        "* [ ] Wrong\n"
        "* [x] Right\n"
    )
    result = quiz_progress.parse_quiz(quiz)
    assert len(result["questions"]) == 1
    assert result["questions"][0]["correct_index"] == 1


def test_parse_quiz_accepts_loose_whitespace_in_checkbox_bugfix_3():
    quiz = (
        "# Quiz\n\n"
        "## Q1: Test?\n\n"
        "-[]Wrong\n"
        "-[X]Right\n"
    )
    result = quiz_progress.parse_quiz(quiz)
    assert len(result["questions"]) == 1
    assert result["questions"][0]["correct_index"] == 1
    assert result["questions"][0]["options"] == ["Wrong", "Right"]


def test_parse_quiz_skips_question_with_no_options():
    quiz = "# Quiz\n\n## Q1: No options here?\n\nJust prose, no checkboxes.\n"
    result = quiz_progress.parse_quiz(quiz)
    assert result["questions"] == []


def test_parse_quiz_skips_question_with_no_marked_answer():
    quiz = "# Quiz\n\n## Q1: Nothing marked?\n\n- [ ] A\n- [ ] B\n"
    result = quiz_progress.parse_quiz(quiz)
    assert result["questions"] == []


def test_parse_quiz_skips_question_with_ambiguous_multiple_marks():
    quiz = "# Quiz\n\n## Q1: Two marked?\n\n- [x] A\n- [x] B\n"
    result = quiz_progress.parse_quiz(quiz)
    assert result["questions"] == []


def test_parse_quiz_one_bad_question_does_not_drop_the_rest():
    quiz = (
        "# Quiz\n\n"
        "## Q1: Broken?\n\n- [ ] A\n- [ ] B\n\n"
        "## Q2: Fine?\n\n- [ ] Wrong\n- [x] Right\n"
    )
    result = quiz_progress.parse_quiz(quiz)
    assert len(result["questions"]) == 1
    assert result["questions"][0]["question"] == "Q2: Fine?"


def test_parse_quiz_default_title_when_no_h1():
    quiz = "## Q1: Only a question?\n\n- [x] A\n- [ ] B\n"
    result = quiz_progress.parse_quiz(quiz)
    assert result["title"] == "Untitled Quiz"


# ---------------------------------------------------------------------
# grade_quiz
# ---------------------------------------------------------------------

def test_grade_quiz_computes_score_and_percent():
    result = quiz_progress.grade_quiz(_SIMPLE_QUIZ, answers=[1, 0])
    assert result["score"] == 1
    assert result["total"] == 2
    assert result["percent"] == 50.0


def test_grade_quiz_all_correct():
    result = quiz_progress.grade_quiz(_SIMPLE_QUIZ, answers=[1, 1])
    assert result["score"] == 2
    assert result["percent"] == 100.0


def test_grade_quiz_missing_trailing_answers_counts_as_unanswered():
    result = quiz_progress.grade_quiz(_SIMPLE_QUIZ, answers=[1])
    assert result["results"][1]["given_index"] is None
    assert result["results"][1]["is_correct"] is False
    assert result["score"] == 1


def test_grade_quiz_none_entry_counts_as_unanswered():
    result = quiz_progress.grade_quiz(_SIMPLE_QUIZ, answers=[None, 1])
    assert result["results"][0]["is_correct"] is False


def test_grade_quiz_zero_gradable_questions_has_zero_percent_not_a_division_error():
    quiz = "# Empty Quiz\n\nNo questions here at all.\n"
    result = quiz_progress.grade_quiz(quiz, answers=[])
    assert result["total"] == 0
    assert result["percent"] == 0.0


def test_grade_quiz_results_carry_full_shape():
    result = quiz_progress.grade_quiz(_SIMPLE_QUIZ, answers=[1, 1])
    r0 = result["results"][0]
    assert set(r0.keys()) == {"question", "options", "correct_index", "given_index",
                                "is_correct", "explanation"}


# ---------------------------------------------------------------------
# record_attempt / list_attempts / get_attempt
# ---------------------------------------------------------------------

def test_record_attempt_persists_and_returns_full_shape(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    attempt = quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ,
                                            answers=[1, 1], created_by="user_1")
    assert attempt["workspace_id"] == "ws_1"
    assert attempt["score"] == 2
    assert attempt["attempt_id"].startswith("attempt_")


def test_record_attempt_is_retrievable_via_list_attempts(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [1, 1], "user_1")
    attempts = quiz_progress.list_attempts("ws_1")
    assert len(attempts) == 1


def test_list_attempts_scopes_by_workspace(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [1, 1], "user_1")
    quiz_progress.record_attempt("ws_2", "node:ws_2:quiz1", _SIMPLE_QUIZ, [1, 1], "user_1")
    assert len(quiz_progress.list_attempts("ws_1")) == 1
    assert len(quiz_progress.list_attempts("ws_2")) == 1


def test_list_attempts_scopes_by_quiz_node_id(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [1, 1], "user_1")
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz2", _SIMPLE_QUIZ, [1, 1], "user_1")
    result = quiz_progress.list_attempts("ws_1", quiz_node_id="node:ws_1:quiz1")
    assert len(result) == 1
    assert result[0]["quiz_node_id"] == "node:ws_1:quiz1"


def test_get_attempt_returns_matching_record(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    attempt = quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [1, 1], "user_1")
    found = quiz_progress.get_attempt(attempt["attempt_id"])
    assert found["attempt_id"] == attempt["attempt_id"]


def test_get_attempt_raises_file_not_found_for_unknown_id(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    try:
        quiz_progress.get_attempt("attempt_does_not_exist")
        assert False, "expected FileNotFoundError"
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------
# get_missed_questions — the "latest result per question" reduce
# ---------------------------------------------------------------------

def test_get_missed_questions_returns_wrong_answers(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [0, 0], "user_1")
    missed = quiz_progress.get_missed_questions("ws_1", "node:ws_1:quiz1")
    assert len(missed) == 2


def test_get_missed_questions_a_later_correct_attempt_clears_a_question(monkeypatch, tmp_path):
    """The core regression case: a question the user has since gotten
    right must not linger in the missed list forever."""
    _use_tmp_path(monkeypatch, tmp_path)
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [0, 0], "user_1")
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [1, 0], "user_1")

    missed = quiz_progress.get_missed_questions("ws_1", "node:ws_1:quiz1")

    missed_questions = {m["question"] for m in missed}
    assert missed_questions == {"Q2: What is 2 + 2?"}


def test_get_missed_questions_empty_when_all_correct(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    quiz_progress.record_attempt("ws_1", "node:ws_1:quiz1", _SIMPLE_QUIZ, [1, 1], "user_1")
    assert quiz_progress.get_missed_questions("ws_1", "node:ws_1:quiz1") == []


def test_get_missed_questions_no_attempts_returns_empty(monkeypatch, tmp_path):
    _use_tmp_path(monkeypatch, tmp_path)
    assert quiz_progress.get_missed_questions("ws_1", "node:ws_1:quiz1") == []
