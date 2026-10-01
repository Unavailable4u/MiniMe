"""
tests/unit/test_eo_code_proposals_wireframe.py — W8.6.

Covers eo/code_proposals.py's create_wireframe_proposal(): a wireframe's
HTML becoming an ordinary pending proposal for index.html, and then
behaving like every other proposal.

What matters most, worst-silent-failure first:

  1. Creating the proposal writes NOTHING to workspace_code_files — only
     resolve_proposal()'s Keep does. (The whole point of routing it
     through the review.)
  2. The preview's own bookkeeping never reaches a saved file: a stray
     `data-mm` attribute is stripped, and a ```html fence is removed, so
     index.html is the wireframe and nothing else (plan §7.3).
  3. Unusable html raises ValueError BEFORE any row is stored.
  4. Overwriting an existing index.html is a `replace` against what was
     saved, flagged `replaces_existing` for the UI — and Keep on it is
     a normal base_version-checked write.
  5. A re-send supersedes the earlier pending WIREFRAME proposal and
     nothing else (not a chat edit, not a deploy config).
  6. The proposal is tagged model_meta.generator == "wireframe_to_code"
     and its event payload is ids/status only (Pusher's 10,240-byte cap).

Isolation: the same in-memory workspace_code_proposals stand-in and fake
workspace file store test_eo_code_proposals_deploy.py builds (its `store`
fixture), so create/list/resolve all run for real.
"""
import pytest

from eo import code_proposals
from relay.emitter import EventType
from tests.unit.test_eo_code_proposals_deploy import FLY_PLAN, store  # noqa: F401  (fixture)

WS = "ws_1"
USER = "user_1"
SESSION = "chat_1"

HTML = (
    "<!doctype html>\n<html><head><title>Login</title></head>\n"
    "<body><button class=\"btn\">Sign in</button></body></html>"
)


def _propose(html=HTML, label="Login screen", session=SESSION):
    return code_proposals.create_wireframe_proposal(WS, html, label, session, USER)


# ---------------------------------------------------------------------------
# 1. The wireframe becomes a pending proposal for index.html; nothing is written
# ---------------------------------------------------------------------------
class TestCreate:
    def test_new_project_gets_a_pending_create_with_the_wireframe(self, store):
        proposal = _propose()
        assert proposal["status"] == "pending"
        assert proposal["session_id"] == SESSION
        assert proposal["summary"] == "Add index.html from wireframe: Login screen"
        [f] = proposal["files"]
        assert f["path"] == "index.html"
        assert f["op"] == "create"
        assert f["base_version"] == 0
        assert f["original"] == ""
        assert f["proposed"] == HTML + "\n"

    def test_creating_writes_nothing_to_the_workspace_files(self, store):
        _propose()
        assert store.writes == []
        assert store.deletes == []

    def test_existing_index_html_is_a_replace_against_what_was_saved(self, store):
        store.files["index.html"] = {"version": 4, "content": "<p>real app</p>\n"}
        proposal = _propose()
        [f] = proposal["files"]
        assert f["op"] == "replace"
        assert f["base_version"] == 4
        assert f["original"] == "<p>real app</p>\n"
        assert f["proposed"] == HTML + "\n"
        assert proposal["summary"] == "Replace index.html from wireframe: Login screen"
        assert proposal["model_meta"]["replaces_existing"] is True

    def test_tagged_so_the_ui_can_tell_it_from_a_chat_edit(self, store):
        meta = _propose()["model_meta"]
        assert meta["generator"] == code_proposals.WIREFRAME_CODE_GENERATOR == "wireframe_to_code"
        assert meta["screen_label"] == "Login screen"
        assert meta["target_path"] == "index.html"
        assert meta["replaces_existing"] is False

    def test_stored_ref_is_index_html(self, store):
        [ref] = _propose()["refs"]
        assert ref["kind"] == "file"
        assert ref["path"] == "index.html"

    def test_instruction_names_the_wireframe(self, store):
        assert _propose()["instruction"] == 'Turn the wireframe "Login screen" into index.html'

    def test_session_id_is_optional(self, store):
        assert _propose(session=None)["session_id"] is None

    def test_ready_event_and_audit_follow_the_normal_proposal_flow(self, store):
        proposal = _propose()
        assert store.audits == [("code_proposal.create",
                                 {"proposal_id": proposal["id"], "status": "pending",
                                  "file_count": 1})]
        [(event, payload)] = store.events
        assert event == EventType.CODE_PROPOSAL_READY
        # ids/status only — never file content (Pusher's byte cap)
        assert payload == {"workspace_id": WS, "proposal_id": proposal["id"], "status": "pending"}


# ---------------------------------------------------------------------------
# 2. What ends up in the file: the wireframe, and only the wireframe
# ---------------------------------------------------------------------------
class TestSource:
    def test_fenced_html_block_is_unfenced(self, store):
        assert _propose("```html\n" + HTML + "\n```")["files"][0]["proposed"] == HTML + "\n"

    def test_fence_with_no_language_is_unfenced_too(self, store):
        assert _propose("```\n<p>x</p>\n```\n")["files"][0]["proposed"] == "<p>x</p>\n"

    def test_a_fence_inside_the_document_is_left_alone(self, store):
        # Only a fence wrapping the WHOLE text is the model's output wrapper.
        html = "<pre>```js\nlet a = 1;\n```</pre>"
        assert _propose(html)["files"][0]["proposed"] == html + "\n"

    @pytest.mark.parametrize("html, expected", [
        ('<div data-mm="wireframe.html:3:0:3:20">hi</div>', "<div>hi</div>"),
        ("<div data-mm='wireframe.html:3:0:3:20' class=\"a\">hi</div>", '<div class="a">hi</div>'),
        ('<div class="a"\n     data-mm="index.html:1:0:1:9">hi</div>', '<div class="a">hi</div>'),
        ('<p DATA-MM="x:1:0:1:5">a</p><p data-mm="x:2:0:2:5">b</p>', "<p>a</p><p>b</p>"),
    ])
    def test_preview_source_attributes_never_reach_a_saved_file(self, store, html, expected):
        assert _propose(html)["files"][0]["proposed"] == expected + "\n"

    def test_similarly_named_attributes_are_kept(self, store):
        html = '<div data-mmx="1" data-mm-extra="2">hi</div>'
        assert _propose(html)["files"][0]["proposed"] == html + "\n"

    def test_text_that_already_ends_in_a_newline_gets_exactly_one(self, store):
        assert _propose("<p>x</p>\n\n\n")["files"][0]["proposed"] == "<p>x</p>\n"


# ---------------------------------------------------------------------------
# 3. Bad input fails before any row exists
# ---------------------------------------------------------------------------
class TestBadInput:
    @pytest.mark.parametrize("html", [
        "", "   \n\t ", "```html\n\n```", "just words, no markup", None, 42, ["<p>x</p>"],
    ])
    def test_unusable_html_raises_and_stores_nothing(self, store, html):
        with pytest.raises(ValueError):
            _propose(html)
        assert store.db.rows == []
        assert store.events == []
        assert store.audits == []

    def test_oversize_html_raises_and_stores_nothing(self, store):
        too_big = "<p>" + "x" * code_proposals._WIREFRAME_MAX_CHARS + "</p>"
        with pytest.raises(ValueError, match="exceeds"):
            _propose(too_big)
        assert store.db.rows == []

    def test_html_exactly_at_the_cap_is_accepted(self, store):
        body = "x" * (code_proposals._WIREFRAME_MAX_CHARS - len("<p></p>"))
        assert _propose(f"<p>{body}</p>")["status"] == "pending"


class TestLabel:
    @pytest.mark.parametrize("label, expected", [
        (None, "Wireframe"),
        ("", "Wireframe"),
        ("   ", "Wireframe"),
        (7, "Wireframe"),
        ("  Login \n screen\t ", "Login screen"),
        ("x" * 500, "x" * 80),
    ])
    def test_label_is_one_clean_capped_line(self, store, label, expected):
        proposal = _propose(label=label)
        assert proposal["model_meta"]["screen_label"] == expected
        assert "\n" not in proposal["summary"]
        assert "\n" not in proposal["instruction"]


# ---------------------------------------------------------------------------
# 4. Keep is a normal, version-checked write
# ---------------------------------------------------------------------------
class TestResolve:
    def test_keep_writes_index_html_as_a_proposal_snapshot(self, store):
        proposal = _propose()
        code_proposals.resolve_proposal(
            WS, proposal["id"], [{"path": "index.html", "decision": "keep"}], USER)
        assert store.writes == [{
            "path": "index.html", "content": HTML + "\n", "base_version": 0, "source": "proposal",
        }]

    def test_undo_writes_nothing(self, store):
        proposal = _propose()
        code_proposals.resolve_proposal(
            WS, proposal["id"], [{"path": "index.html", "decision": "undo"}], USER)
        assert store.writes == []

    def test_keep_after_index_html_changed_underneath_is_stale(self, store):
        store.files["index.html"] = {"version": 1, "content": "old\n"}
        proposal = _propose()
        store.files["index.html"] = {"version": 2, "content": "someone else's edit\n"}
        with pytest.raises(code_proposals.ProposalStaleError):
            code_proposals.resolve_proposal(
                WS, proposal["id"], [{"path": "index.html", "decision": "keep"}], USER)
        assert store.writes == []


# ---------------------------------------------------------------------------
# 5. A re-send supersedes the earlier wireframe proposal, and only that one
# ---------------------------------------------------------------------------
class TestSupersede:
    def test_second_send_rejects_the_first(self, store):
        first = _propose(label="v1")
        second = _propose(label="v2")
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[first["id"]]["status"] == "rejected"
        assert rows[first["id"]]["resolved_at"] is not None
        assert rows[second["id"]]["status"] == "pending"
        assert store.writes == []

    def test_a_chat_edit_proposal_is_left_alone(self, store):
        chat = code_proposals.create_proposal(
            WS, "make it blue", [{"kind": "file", "path": "index.html", "provider": "cloud"}],
            SESSION, USER,
            generate_edit=lambda ws, instr, refs, cur: {
                "summary": "blue", "files": [{"path": "index.html", "op": "create", "content": "<b>"}]},
        )
        wireframe = _propose()
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[chat["id"]]["status"] == "pending"
        assert rows[wireframe["id"]]["status"] == "pending"

    def test_a_deploy_config_proposal_is_left_alone(self, store):
        deploy = code_proposals.create_deploy_config_proposal(WS, FLY_PLAN, SESSION, USER)
        wireframe = _propose()
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[deploy["id"]]["status"] == "pending"
        assert rows[wireframe["id"]]["status"] == "pending"

    def test_a_deploy_re_propose_does_not_reject_a_wireframe_proposal(self, store):
        wireframe = _propose()
        code_proposals.create_deploy_config_proposal(WS, FLY_PLAN, SESSION, USER)
        code_proposals.create_deploy_config_proposal(WS, FLY_PLAN, SESSION, USER)
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[wireframe["id"]]["status"] == "pending"

    def test_a_bad_resend_never_costs_the_person_the_earlier_proposal(self, store):
        first = _propose()
        with pytest.raises(ValueError):
            _propose("")
        rows = {r["id"]: r for r in store.db.rows}
        assert rows[first["id"]]["status"] == "pending"
