"""Patch C26: the folder-trust decision inside the terminal chat -- when the prompt appears, what each answer does, and that
the project layer is read only after (and only if) the person said trust.

Real: the session, the C06 policy engine, the rules loader, the tool runner and the trust store (under the hermetic
$M2C_HOME).  Fake: the server and the person (keys are sent with ``on_key`` / ``on_escape``, and once through a real
prompt_toolkit input).
"""
import asyncio
import hashlib
import os
import stat

import pytest

from tui_rig import Chat, FakeUI, Rig, call_id, run_async

from m2c.policy import rules as rules_mod
from m2c.trust.fingerprint import compute_fingerprint
from m2c.trust.store import TrustStore
from m2c.tui import session as session_mod
from m2c.tui.session import Session

SETTINGS = '[permissions]\nallow = ["bash(echo:*)"]\n'
FILES = {".m2c/settings.toml": SETTINGS}
WARNING = "hooks run commands on your machine"


@pytest.fixture
def layer_loads(monkeypatch):
    """Every call of the rules loader's file reader: (layer name, path)."""
    calls = []
    real = rules_mod.load_rule_file

    def spy(path, layer):
        calls.append((layer, None if path is None else str(path)))
        return real(path, layer)

    monkeypatch.setattr(rules_mod, "load_rule_file", spy)
    return calls


def project_loads(calls):
    return [c for c in calls if c[0] == "project" and c[1] is not None]


def echo_handler(cid):
    async def handler(chat, msg):
        run = msg["run_id"]
        if msg["t"] == "user_message":
            await chat.emit(run, type="state_changed", state="running")
            await chat.propose(run, cid, "bash", {"command": "echo hi"}, "R3")
        elif msg["t"] == "tool_result":
            await chat.finish(run, cid, "bash", "ok")
            await chat.say(run, "done")
        elif msg["t"] == "approval" and msg["payload"].get("decision") != "allow":
            await chat.finish(run, cid, "bash", "denied")
    return handler


def another_session(rig: Rig) -> Session:
    """A second start of the same project: the trust decision is made in the constructor."""
    return Session(rig.store, FakeUI(), project_path=str(rig.project))


def trust_entry(rig: Rig):
    return TrustStore.default().get(rig.project.resolve())


# ---------------------------------------------------------------------------------------------------- the prompt
def test_a_folder_with_project_settings_asks_once_at_startup(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            s = rig.session
            assert s.trust.needs_prompt is True and s.trust.reason == "never_asked" and s.trust_prompt_active
            text = rig.ui.text()
            assert WARNING in text and ".m2c/settings.toml" in text and str(rig.project.resolve()) in text
            assert text.count(WARNING) == 1
            assert s.policy_context().project_trusted is False
            assert project_loads(layer_loads) == []
    run_async(scenario())


def test_the_prompt_is_shown_once_not_again_on_later_events(tmp_path):
    cid = call_id()

    async def scenario():
        async with Rig(tmp_path, Chat(echo_handler(cid)), files=FILES) as rig:
            rig.session.on_key("n")
            await rig.send("go")
            await rig.until(lambda: rig.session.approval_active, "a prompt")
            rig.session.on_key("n")
            assert rig.ui.text().count(WARNING) == 1
    run_async(scenario())


def test_no_project_files_means_no_prompt_and_no_note(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(), files={"src/app.py": "x"}) as rig:
            s = rig.session
            assert s.trust.reason == "nothing_to_trust" and not s.trust_prompt_active
            assert WARNING not in rig.ui.text() and not any("ignored" in l for l in rig.ui.status)
            assert s.captures_keys(True) is False
    run_async(scenario())


def test_the_prompt_comes_before_the_hello_line_and_before_any_project_file_is_read(tmp_path, layer_loads):
    events = []

    async def scenario():
        rig = Rig(tmp_path, Chat(), files=FILES)
        real_show = rig.ui.show

        def show(blocks):
            for b in blocks:
                if getattr(b, "text", "") and WARNING in b.text:
                    events.append(("prompt", len(project_loads(layer_loads))))
                if getattr(b, "text", "").startswith("m2c: type a message"):
                    events.append(("hello", len(project_loads(layer_loads))))
            real_show(blocks)

        rig.ui.show = show
        async with rig:
            assert events and events[0] == ("prompt", 0)               # shown while nothing of the project was read
            assert [e[0] for e in events][:2] == ["prompt", "hello"]
            rig.session.on_key("t")
            assert len(project_loads(layer_loads)) == 1                # read only after the answer
    run_async(scenario())


# ---------------------------------------------------------------------------------------------------- the keys
def test_t_trusts_remembers_and_only_then_loads_the_project_layer(tmp_path, layer_loads):
    cid = call_id()

    async def scenario():
        async with Rig(tmp_path, Chat(echo_handler(cid)), files=FILES) as rig:
            s = rig.session
            assert s.captures_keys(True) is True and s.captures_keys(False) is True
            s.on_key("t")
            assert not s.trust_prompt_active and s.project_trusted is True
            assert s.policy_context().project_trusted is True
            loads = project_loads(layer_loads)
            assert len(loads) == 1 and loads[0][1].endswith(os.path.join(".m2c", "settings.toml"))
            entry = trust_entry(rig)
            assert entry.decision == "trusted" and entry.fingerprint == compute_fingerprint(rig.project).digest
            assert s.captures_keys(True) is False
            assert not any("ignored" in l for l in rig.ui.status)
            # the project's allow rule now takes effect: no approval prompt, the command runs
            await rig.send("go")
            await rig.until(lambda: len(rig.chat.results) == 1, "a result without any approval prompt")
            assert not s.approval_active                      # nothing was waiting for a key: the rule allowed it
            assert [a["decision"] for a in rig.chat.approvals] == ["allow"]
    run_async(scenario())


def test_s_trusts_for_this_session_only_and_remembers_nothing(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_key("s")
            assert rig.session.project_trusted is True and len(project_loads(layer_loads)) == 1
            assert trust_entry(rig) is None
            assert not TrustStore.default().path.exists()
            again = another_session(rig)
            assert again.trust.needs_prompt is True and again.trust.reason == "never_asked"
            assert again.policy_context().project_trusted is False
    run_async(scenario())


def test_n_ignores_the_project_and_remembers_the_decline(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_key("n")
            assert not rig.session.trust_prompt_active and rig.session.policy_context().project_trusted is False
            assert project_loads(layer_loads) == []
            assert trust_entry(rig).decision == "untrusted"
            assert any("project settings (.m2c/settings.toml) are ignored" in l for l in rig.ui.status)
            again = another_session(rig)
            assert again.trust.reason == "declined" and again.trust.needs_prompt is False
            assert again.policy_context().project_trusted is False
            assert project_loads(layer_loads) == []
    run_async(scenario())


def test_escape_is_the_same_as_n_and_does_not_stop_anything_else(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_escape()
            assert not rig.session.trust_prompt_active and trust_entry(rig).decision == "untrusted"
            assert rig.session.project_trusted is False and project_loads(layer_loads) == []
            assert rig.chat.cancels == [] and not rig.ui.has("Rewind")
    run_async(scenario())


@pytest.mark.parametrize("key", ["y", "x", "enter", "\r", " ", "tab", "g", "e", "d", "a"])
def test_other_keys_leave_the_prompt_open_and_the_project_ignored(tmp_path, key, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_key(key)
            assert rig.session.trust_prompt_active and rig.session.project_trusted is False
            assert trust_entry(rig) is None and project_loads(layer_loads) == []
    run_async(scenario())


def test_ctrl_c_with_the_question_open_leaves_and_remembers_nothing(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_interrupt()
            assert rig.session.exit_requested.is_set() and trust_entry(rig) is None
    run_async(scenario())


def test_a_message_sent_before_the_answer_runs_with_the_project_ignored(tmp_path, layer_loads):
    cid = call_id()

    async def scenario():
        async with Rig(tmp_path, Chat(echo_handler(cid)), files=FILES) as rig:
            await rig.send("go")
            await rig.until(lambda: rig.session.approval_active, "a prompt although the project file allows it")
            assert project_loads(layer_loads) == []
            rig.session.on_key("n")
    run_async(scenario())


# ---------------------------------------------------------------------------------------------------- remembered decisions
def test_a_remembered_trust_is_applied_at_the_next_start_without_a_prompt(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_key("t")
            layer_loads.clear()
            again = another_session(rig)
            assert again.trust.trusted is True and again.trust.reason == "trusted" and again.trust.needs_prompt is False
            assert again.policy_context().project_trusted is True and len(project_loads(layer_loads)) == 1
            assert not any("ignored" in l for l in again.status.notes)
    run_async(scenario())


def test_a_file_changed_between_starts_asks_again_and_is_not_read_until_answered(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_key("t")
            (rig.project / ".m2c" / "settings.toml").write_text('[permissions]\nallow = ["bash(curl:*)"]\n')
            layer_loads.clear()
            again = another_session(rig)
            assert again.trust.reason == "changed" and again.trust.needs_prompt is True
            assert again.policy_context().project_trusted is False and project_loads(layer_loads) == []
    run_async(scenario())


def test_a_new_hook_file_changes_the_decision_even_though_settings_did_not(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_key("t")
            (rig.project / ".m2c" / "hooks").mkdir()
            (rig.project / ".m2c" / "hooks" / "pre.sh").write_text("echo hi")
            assert another_session(rig).trust.reason == "changed"
    run_async(scenario())


def test_the_prompt_for_a_changed_folder_says_it_changed(tmp_path):
    async def scenario():
        rig = Rig(tmp_path, Chat(), files=FILES)
        TrustStore.default().set(rig.project, "trusted", hashlib.sha256(b"older files").hexdigest())
        async with rig:
            assert rig.session.trust.reason == "changed" and rig.ui.has("changed since")
    run_async(scenario())


# ---------------------------------------------------------------------------------------------------- never trusted automatically
def test_a_symlink_in_the_project_is_never_trusted_and_there_is_no_prompt(tmp_path, layer_loads):
    async def scenario():
        rig = Rig(tmp_path, Chat(), files=FILES)
        outside = tmp_path / "outside"
        outside.mkdir()
        os.symlink(outside, rig.project / ".m2c" / "skills")
        TrustStore.default().set(rig.project, "trusted", compute_fingerprint(rig.project).digest or "0" * 64)
        async with rig:
            s = rig.session
            assert s.trust.reason == "symlink" and not s.trust_prompt_active and s.project_trusted is False
            assert WARNING not in rig.ui.text() and project_loads(layer_loads) == []
            assert any("symlink" in l.lower() for l in rig.ui.status)
    run_async(scenario())


def test_an_oversized_tree_is_never_trusted_and_there_is_no_prompt(tmp_path, monkeypatch, layer_loads):
    monkeypatch.setattr("m2c.trust.fingerprint.MAX_FILES", 2)
    files = {".m2c/settings.toml": SETTINGS, ".m2c/rules/a.md": "a", ".m2c/rules/b.md": "b"}

    async def scenario():
        async with Rig(tmp_path, Chat(), files=files) as rig:
            assert rig.session.trust.reason == "too_large" and not rig.session.trust_prompt_active
            assert rig.session.project_trusted is False and project_loads(layer_loads) == []
    run_async(scenario())


def test_an_unusable_store_means_untrusted_with_the_fix_shown_and_no_prompt(tmp_path, layer_loads):
    async def scenario():
        rig = Rig(tmp_path, Chat(), files=FILES)
        store = TrustStore.default()
        store.set(rig.project, "trusted", compute_fingerprint(rig.project).digest)
        store.path.chmod(0o644)
        async with rig:
            s = rig.session
            assert s.trust.reason == "store_unusable" and not s.trust_prompt_active and s.project_trusted is False
            assert rig.ui.has(f"chmod 600 {store.path}")                       # the full fix is shown ...
            assert any("trust file cannot be used" in l for l in rig.ui.status)  # ... and one status line points at it
            assert project_loads(layer_loads) == []
            assert stat.S_IMODE(store.path.stat().st_mode) == 0o644         # reading never "fixes" it
    run_async(scenario())


# ---------------------------------------------------------------------------------------------------- never upgrades itself
def test_the_session_never_upgrades_itself_from_untrusted_to_trusted(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            s = rig.session
            s.on_key("n")
            # the store is changed behind its back, the files change, and the person presses t later: nothing trusts
            TrustStore.default().set(rig.project, "trusted", compute_fingerprint(rig.project).digest)
            (rig.project / ".m2c" / "hooks").mkdir()
            (rig.project / ".m2c" / "hooks" / "x.sh").write_text("echo hi")
            for key in ("t", "s", "t"):
                s.on_key(key)
            s.on_escape()
            assert s.project_trusted is False and s.policy_context().project_trusted is False
            assert project_loads(layer_loads) == [] and not s.trust_prompt_active
    run_async(scenario())


def test_an_answer_is_applied_once(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            rig.session.on_key("s")
            rig.session.on_key("n")
            rig.session.on_key("t")
            assert rig.session.project_trusted is True and len(project_loads(layer_loads)) == 1
            assert trust_entry(rig) is None
    run_async(scenario())


def test_files_changed_while_the_question_was_open_are_not_trusted(tmp_path, layer_loads):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            (rig.project / ".m2c" / "settings.toml").write_text('[permissions]\nallow = ["bash(rm:*)"]\n')
            rig.session.on_key("t")
            assert rig.session.project_trusted is False and not rig.session.trust_prompt_active
            assert trust_entry(rig) is None                       # nothing is remembered for files nobody was shown
            assert project_loads(layer_loads) == []               # and the project layer was never read
            assert rig.ui.has("changed while")
            assert rig.session.policy_context().project_trusted is False
    run_async(scenario())


def test_a_file_changed_between_the_fingerprint_and_the_read_discards_the_project_layer(tmp_path, monkeypatch):
    """The project layer is verified AFTER it is parsed: if the files moved in between, it is dropped."""
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            real = rules_mod.load_rule_file

            def tamper(path, layer):
                result = real(path, layer)
                if layer == "project" and path is not None:
                    (rig.project / ".m2c" / "rules").mkdir(exist_ok=True)
                    (rig.project / ".m2c" / "rules" / "late.md").write_text("added while loading")
                return result

            monkeypatch.setattr(rules_mod, "load_rule_file", tamper)
            rig.session.on_key("t")
            assert rig.session.project_trusted is False
            assert rig.session.policy_context().project_trusted is False
            assert trust_entry(rig) is None
    run_async(scenario())


def test_a_store_that_cannot_be_written_still_trusts_this_session_and_says_so(tmp_path, monkeypatch):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            def broken(self, *a, **k):
                from m2c.trust.store import TrustStoreError
                raise TrustStoreError("Could not save the trust file /x/trust.json. Check free space and permissions.")

            monkeypatch.setattr(TrustStore, "set", broken)
            rig.session.on_key("t")
            assert rig.session.project_trusted is True
            assert rig.ui.has("this session only") and rig.ui.has("Could not save")
    run_async(scenario())


# ---------------------------------------------------------------------------------------------------- the status line
def test_the_ignored_line_is_there_while_untrusted_and_gone_after_trust(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            assert any("project settings (.m2c/settings.toml) are ignored" in l for l in rig.ui.status)
            assert sum("ignored" in l for l in rig.ui.status) == 1
            rig.session.on_key("t")
            assert not any("ignored" in l for l in rig.ui.status)
    run_async(scenario())


def test_the_old_wording_about_this_version_is_gone(tmp_path):
    async def scenario():
        async with Rig(tmp_path, Chat(), files=FILES) as rig:
            assert not any("in this version" in l for l in rig.ui.status)
    run_async(scenario())


# ---------------------------------------------------------------------------------------------------- through a real terminal
def test_a_real_terminal_shows_the_prompt_and_t_is_a_key_not_text(tmp_path):
    pytest.importorskip("prompt_toolkit")
    pytest.importorskip("rich")
    from test_tui_app_e2e import Terminal

    async def scenario():
        async with Terminal(tmp_path, Chat(), files=FILES) as term:
            await term.wait_for(WARNING)
            session = term.rig.session
            assert session.trust_prompt_active
            await term.type("x")                                    # a wrong key: the prompt stays
            assert session.trust_prompt_active
            await term.type("t")
            await term.rig.until(lambda: not session.trust_prompt_active, "the prompt to close")
            assert session.project_trusted is True
            assert trust_entry(term.rig).decision == "trusted"
            await term.type("hi")                                   # now keys are text again, not commands
            assert session.captures_keys(False) is False
    run_async(scenario())


def test_a_real_terminal_escape_declines(tmp_path):
    pytest.importorskip("prompt_toolkit")
    pytest.importorskip("rich")
    from test_tui_app_e2e import ESC, Terminal

    async def scenario():
        async with Terminal(tmp_path, Chat(), files=FILES) as term:
            await term.wait_for(WARNING)
            await term.type(ESC, settle=0.5)
            await term.rig.until(lambda: not term.rig.session.trust_prompt_active, "the prompt to close")
            assert term.rig.session.project_trusted is False
            assert trust_entry(term.rig).decision == "untrusted"
    run_async(scenario())
