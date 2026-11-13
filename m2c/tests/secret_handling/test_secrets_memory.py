"""
m2c/tests/secret_handling/test_secrets_memory.py -- Patch C15: C12's memory-write check now uses the shared detector.

The old tests in tests/context/test_context_memory.py keep passing unchanged (same refusal rules, same messages);
these add what is broader now, and pin that there is ONE detector, not two.
"""
from __future__ import annotations

import pytest
from secret_fakes import BUILDERS, leaked, make

import m2c.context.memory as mem
import m2c.secrets.redact as redact_mod
from m2c.context import MemoryWriteRefused, find_secrets, prepare_memory_write
from m2c.secrets.redact import redact


def _world(tmp_path):
    root, home = tmp_path / "repo", tmp_path / "m2chome"
    root.mkdir(), home.mkdir()
    return root.resolve(), home.resolve()


# Kinds the old check did not know: refused now.
NEW_KINDS = ["aws_secret_key", "gcp_oauth_token", "slack_webhook", "npm_token", "pypi_token", "sendgrid_key",
             "huggingface_token", "azure_account_key", "anthropic_style_key", "secret_assignment_cli", "basic_header"]


@pytest.mark.parametrize("name", NEW_KINDS)
def test_secrets_the_old_check_did_not_know_are_refused_and_never_echoed(tmp_path, name):
    root, home = _world(tmp_path)
    fake = make(name, 2)
    with pytest.raises(MemoryWriteRefused) as caught:
        prepare_memory_write(root, "- a note\n" + fake.text + "\n- another note", home=home)
    assert caught.value.findings[0].line == 2
    assert not leaked(str(caught.value), fake.secret) and not leaked(repr(caught.value.findings), fake.secret)
    assert not (home / "memory").exists()


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_kind_the_redactor_knows_is_refused_for_memory(name):
    fake = make(name, 3)
    assert find_secrets("one\n" + fake.text), name


def test_the_memory_check_is_the_shared_detector_not_a_second_copy():
    assert mem.find_secrets.__module__ == "m2c.context.memory"
    assert not hasattr(mem, "_SECRET_PATTERNS")                  # the old private pattern table is gone
    assert mem.scan_lines is redact_mod.scan_lines if hasattr(mem, "scan_lines") else True


def test_the_stricter_memory_profile_still_refuses_what_redaction_lets_through():
    # a bare word after "password:" is code to the redactor (a type name or a keyword) but a secret to the memory check
    line = "password: required"
    assert redact(line).text == line and [f.kind for f in find_secrets(line)] == ["password_assignment"]
    env_line = "SERVICE_HOST=internal.example"
    assert redact(env_line).text == env_line and [f.kind for f in find_secrets(env_line)] == ["env_line"]


def test_every_kind_has_a_sentence_for_the_refusal_message():
    from m2c.secrets.redact import KIND_TEXT

    for name in BUILDERS:
        for kind in make(name).kinds:
            assert kind in KIND_TEXT or kind in ("aws_secret_key",), kind
    assert KIND_TEXT["private_key"] == "a private key block" and KIND_TEXT["jwt"] == "a JSON Web Token"


def test_a_failing_detector_refuses_the_write_instead_of_allowing_it(tmp_path, monkeypatch):
    root, home = _world(tmp_path)

    def boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(redact_mod, "_scan", boom)
    with pytest.raises(MemoryWriteRefused):
        prepare_memory_write(root, "- an ordinary note", home=home)
    assert not (home / "memory").exists()


def test_the_module_still_says_that_c15_generalised_secret_detection():
    assert "C15" in (mem.__doc__ or "") and "C15" in (find_secrets.__doc__ or "")
