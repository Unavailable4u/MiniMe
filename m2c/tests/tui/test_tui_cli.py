"""Patch C09b: how ``m2c`` chooses between the terminal chat and the plain prompt, and what it refuses."""
import subprocess
import sys

import pytest

from m2c import cli


class FakeTTY:
    def isatty(self):
        return True

    def write(self, text):
        return len(text)

    def flush(self):
        pass


@pytest.fixture
def signed_in(monkeypatch):
    from m2c.auth import credentials
    monkeypatch.setattr(credentials, "load_credential", lambda *a, **k: object())


def record_plain(monkeypatch):
    from m2c import repl
    seen = {}

    async def fake_run_prompt(store, *, project_path=None, **kwargs):
        seen["plain"] = project_path
        return 0

    monkeypatch.setattr(repl, "run_prompt", fake_run_prompt)
    return seen


def record_chat(monkeypatch):
    pytest.importorskip("prompt_toolkit")
    from m2c.tui import app
    seen = {}

    async def fake_run_chat(store, *, project_path, mode="ask", **kwargs):
        seen["chat"] = (project_path, mode)
        return 0

    monkeypatch.setattr(app, "run_chat", fake_run_chat)
    return seen


def make_tty(monkeypatch):
    monkeypatch.setattr(sys, "stdin", FakeTTY())
    monkeypatch.setattr(sys, "stdout", FakeTTY())


def test_a_terminal_opens_the_chat_in_ask_mode_by_default(monkeypatch, signed_in):
    seen = record_chat(monkeypatch)
    plain = record_plain(monkeypatch)
    make_tty(monkeypatch)
    assert cli.main([]) == cli.EXIT_OK
    assert seen["chat"][1] == "ask" and plain == {}


@pytest.mark.parametrize("mode", ["plan", "ask", "accept-edits"])
def test_mode_chooses_the_startup_mode_of_the_chat(monkeypatch, signed_in, mode):
    seen = record_chat(monkeypatch)
    make_tty(monkeypatch)
    assert cli.main(["--mode", mode]) == cli.EXIT_OK
    assert seen["chat"][1] == mode


def test_plain_uses_the_plain_prompt_even_on_a_terminal(monkeypatch, signed_in):
    chat = record_chat(monkeypatch)
    plain = record_plain(monkeypatch)
    make_tty(monkeypatch)
    assert cli.main(["--plain"]) == cli.EXIT_OK
    assert "plain" in plain and chat == {}


def test_without_a_terminal_the_plain_prompt_is_used(monkeypatch, signed_in):
    chat = record_chat(monkeypatch)
    plain = record_plain(monkeypatch)
    assert cli.main([]) == cli.EXIT_OK            # pytest's stdin is not a terminal
    assert "plain" in plain and chat == {}


@pytest.mark.parametrize("args", [
    ["--mode", "plan", "--plain"],          # the plain prompt has no modes: a safety option is never ignored
    ["--mode", "bogus"],
    ["--output-format", "json"],
    ["--plain", "-p", "task"],
    ["--plain", "login"],
    ["--mode", "plan", "doctor"],
])
def test_refused_combinations_are_usage_errors(monkeypatch, signed_in, capsys, args):
    chat = record_chat(monkeypatch)
    plain = record_plain(monkeypatch)
    make_tty(monkeypatch)
    assert cli.main(args) == cli.EXIT_USAGE
    assert chat == {} and plain == {}
    assert capsys.readouterr().err.startswith("m2c: ")


def test_mode_without_a_terminal_is_refused_not_ignored(monkeypatch, signed_in, capsys):
    plain = record_plain(monkeypatch)
    assert cli.main(["--mode", "plan"]) == cli.EXIT_USAGE
    assert plain == {} and "needs the terminal chat" in capsys.readouterr().err


def test_an_unusable_project_folder_is_a_usage_error_with_fixed_text(monkeypatch, signed_in, capsys, tmp_path):
    pytest.importorskip("prompt_toolkit")
    make_tty(monkeypatch)
    monkeypatch.chdir("/")
    assert cli.main([]) == cli.EXIT_USAGE
    assert "cannot be used as a project" in capsys.readouterr().err


def test_not_signed_in_still_exits_4_for_the_chat(monkeypatch, capsys):
    pytest.importorskip("prompt_toolkit")
    make_tty(monkeypatch)
    assert cli.main([]) == cli.EXIT_AUTH
    assert "m2c login" in capsys.readouterr().err


@pytest.mark.parametrize("code", [
    "import m2c.cli, m2c.headless, m2c.repl, sys; "
    "assert not {'prompt_toolkit', 'rich'} & set(sys.modules), sorted(m for m in sys.modules if m.startswith(('prompt_toolkit', 'rich')))",
    "import m2c.tui.session, m2c.tui.render, m2c.tui.approval, m2c.tui.history, sys; "
    "assert not {'prompt_toolkit', 'rich'} & set(sys.modules)",
])
def test_headless_and_the_pure_modules_never_load_the_terminal_libraries(code):
    result = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
