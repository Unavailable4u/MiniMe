"""Patch C26: ``m2c trust --list`` and ``m2c trust --forget PATH``.

The command only reads and edits the trust file (there is no way to grant trust from the command line: trust is given by
answering the prompt in the folder itself).  Nothing secret is involved, but the folder names come from disk, so they are
cleaned before they are printed.
"""
import hashlib
import logging
import os
from pathlib import Path

import pytest

from m2c import cli
from m2c import logging_setup as ls
from m2c.trust.store import TrustStore


@pytest.fixture(autouse=True)
def _clean_m2c_logger():
    yield
    logger = logging.getLogger(ls.LOGGER_NAME)
    for h in list(logger.handlers):
        logger.removeHandler(h)


def fp(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


@pytest.fixture
def store() -> TrustStore:
    return TrustStore.default()


def folder(tmp_path, name) -> Path:
    path = tmp_path / name
    path.mkdir()
    return path.resolve()


# ---------------------------------------------------------------------------------------------------- --list
def test_list_with_no_decisions_says_so_and_creates_nothing(capsys):
    home = Path(os.environ["M2C_HOME"])
    assert cli.main(["trust", "--list"]) == 0
    assert "no folder trust decisions" in capsys.readouterr().out.lower()
    assert not home.exists()


def test_list_shows_each_folder_and_its_decision(tmp_path, store, capsys):
    a, b = folder(tmp_path, "alpha"), folder(tmp_path, "beta")
    store.set(a, "trusted", fp("a"))
    store.set(b, "untrusted", fp("b"))
    assert cli.main(["trust", "--list"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].split()[0] == "trusted" and str(a) in lines[0]
    assert lines[1].split()[0] == "untrusted" and str(b) in lines[1]


def test_list_shows_the_date_and_never_a_fingerprint(tmp_path, capsys):
    TrustStore(TrustStore.default().path, clock=lambda: 1_700_000_000).set(
        folder(tmp_path, "alpha"), "trusted", fp("a-fingerprint"))
    assert cli.main(["trust", "--list"]) == 0
    out = capsys.readouterr().out
    assert "2023-11-14 22:13 UTC" in out and fp("a-fingerprint") not in out


def test_list_cleans_folder_names_before_printing_them(tmp_path, store, capsys):
    weird = folder(tmp_path, "red\x1b[31m-folder")
    store.set(weird, "trusted", fp("w"))
    assert cli.main(["trust", "--list"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "red" in out


def test_list_mentions_entries_it_had_to_ignore(tmp_path, store, capsys):
    store.set(folder(tmp_path, "alpha"), "trusted", fp("a"))
    import json
    data = json.loads(store.path.read_text())
    data["folders"]["/p/bad"] = "trusted"
    store.path.write_text(json.dumps(data))
    store.path.chmod(0o600)
    assert cli.main(["trust", "--list"]) == 0
    out = capsys.readouterr().out
    assert "1 malformed entry ignored" in out


def test_list_refuses_a_loose_file_and_says_how_to_fix_it(tmp_path, store, capsys):
    store.set(folder(tmp_path, "alpha"), "trusted", fp("a"))
    store.path.chmod(0o644)
    assert cli.main(["trust", "--list"]) == cli.EXIT_FAILURE
    err = capsys.readouterr().err
    assert f"chmod 600 {store.path}" in err and "m2c trust" in err


# ---------------------------------------------------------------------------------------------------- --forget
def test_forget_removes_the_decision(tmp_path, store, capsys):
    a, b = folder(tmp_path, "alpha"), folder(tmp_path, "beta")
    store.set(a, "trusted", fp("a"))
    store.set(b, "trusted", fp("b"))
    assert cli.main(["trust", "--forget", str(a)]) == 0
    assert "forgot" in capsys.readouterr().out.lower()
    assert store.get(a) is None and store.get(b) is not None


def test_forget_a_folder_with_no_decision_is_not_an_error(tmp_path, capsys):
    assert cli.main(["trust", "--forget", str(folder(tmp_path, "alpha"))]) == 0
    assert "no decision" in capsys.readouterr().out.lower()
    assert not Path(os.environ["M2C_HOME"]).exists()


def test_forget_resolves_a_relative_path_and_a_symlink(tmp_path, store, monkeypatch, capsys):
    a = folder(tmp_path, "alpha")
    store.set(a, "trusted", fp("a"))
    monkeypatch.chdir(a)
    assert cli.main(["trust", "--forget", "."]) == 0
    assert store.get(a) is None
    store.set(a, "trusted", fp("a"))
    link = tmp_path / "link"
    os.symlink(a, link)
    assert cli.main(["trust", "--forget", str(link)]) == 0
    assert store.get(a) is None


def test_forget_works_for_a_folder_that_was_deleted(tmp_path, store, capsys):
    gone = folder(tmp_path, "gone")
    store.set(gone, "trusted", fp("g"))
    gone.rmdir()
    assert cli.main(["trust", "--forget", str(gone)]) == 0
    assert store.items() == []


def test_forget_with_a_loose_file_refuses_and_changes_nothing(tmp_path, store, capsys):
    a = folder(tmp_path, "alpha")
    store.set(a, "trusted", fp("a"))
    store.path.chmod(0o666)
    before = store.path.read_bytes()
    assert cli.main(["trust", "--forget", str(a)]) == cli.EXIT_FAILURE
    assert store.path.read_bytes() == before and "chmod 600" in capsys.readouterr().err


def test_forget_cleans_the_name_it_echoes(tmp_path, capsys):
    assert cli.main(["trust", "--forget", str(tmp_path / "x\x1b[2Jy")]) == 0
    assert "\x1b" not in capsys.readouterr().out


# ---------------------------------------------------------------------------------------------------- usage
def test_exactly_one_option_is_required(capsys):
    assert cli.main(["trust"]) == cli.EXIT_USAGE
    assert cli.main(["trust", "--list", "--forget", "/x"]) == cli.EXIT_USAGE
    assert cli.main(["trust", "--grant", "/x"]) == cli.EXIT_USAGE


def test_there_is_no_command_line_way_to_grant_trust():
    parser = cli.build_parser()
    help_text = parser.format_help() + "".join(
        action.format_help() for action in parser._subparsers._group_actions[0].choices.values())
    # PATCH C10b: ``m2c -p --allow RULE`` exists now.  It adds a permission rule for one run; it never trusts a folder
    # (headless mode ignores project settings, see tests/headless/test_headless_wiring.py), so it is not a way to grant trust.
    assert "--trust" not in help_text and "--yes" not in help_text


def test_the_command_is_listed_in_the_help(capsys):
    assert cli.main(["--help"]) == 0
    assert "trust" in capsys.readouterr().out


def test_chat_options_do_not_combine_with_the_command(capsys):
    assert cli.main(["--mode", "ask", "trust", "--list"]) == cli.EXIT_USAGE
    assert cli.main(["--plain", "trust", "--list"]) == cli.EXIT_USAGE
