"""
m2c/tests/context/test_context_memory.py -- Patch C12: the auto-memory index
and memory writes.

Written before the implementation.  Groups: the per-project directory key, the
index limits (first 200 lines or 25 KB, whichever is smaller), preparing a
write (never touches the disk), approving it, and refusing secrets.

Fake secrets come from context_helpers and are assembled at run time (see
that module: gitleaks scans this directory).
"""
from __future__ import annotations

import stat

import context_helpers as ch
import pytest
from context_helpers import make_world, put
from m2c.context import memory as mem
from m2c.context.memory import (
    MemoryWriteConflict,
    MemoryWriteNotApproved,
    MemoryWriteRefused,
    PreparedMemoryWrite,
    apply_memory_write,
    find_secrets,
    load_memory_index,
    memory_dir,
    prepare_memory_write,
    project_key,
)


@pytest.fixture
def world(tmp_path):
    return make_world(tmp_path)


def snapshot(path):
    """Every path under ``path`` with its size, to prove nothing was written."""
    return sorted((str(p.relative_to(path)), p.stat().st_size if p.is_file() else -1) for p in path.rglob("*"))


# ---------------------------------------------------------------------------
# the per-project directory
# ---------------------------------------------------------------------------
def test_the_key_is_stable_and_readable(world):
    k1, k2 = project_key(world.root), project_key(world.root)
    assert k1 == k2 and k1.startswith("repo-") and len(k1.split("-")[-1]) == 12


def test_two_projects_with_the_same_folder_name_get_different_keys(tmp_path):
    a, b = tmp_path / "x" / "app", tmp_path / "y" / "app"
    a.mkdir(parents=True), b.mkdir(parents=True)
    assert project_key(a) != project_key(b)


def test_a_symlinked_path_to_the_same_project_has_the_same_key(world, tmp_path):
    alias = tmp_path / "alias"
    alias.symlink_to(world.root, target_is_directory=True)
    assert project_key(alias) == project_key(world.root)


def test_odd_folder_names_give_safe_keys(tmp_path):
    weird = tmp_path / "My Project (v2)! é"
    weird.mkdir()
    key = project_key(weird)
    assert key == key.lower() and key.isascii()
    assert all(c.isalnum() or c in "._-" for c in key) and len(key) <= 60 and not key.startswith(".")


def test_the_filesystem_root_still_gets_a_key():
    assert project_key("/").startswith("project-")


def test_memory_dir_lives_under_home_memory_key(world):
    assert memory_dir(world.root, world.home) == world.home / "memory" / project_key(world.root)


def test_memory_dir_follows_m2c_home_when_home_is_not_passed(world, monkeypatch):
    monkeypatch.setenv("M2C_HOME", str(world.home))
    assert memory_dir(world.root) == world.home / "memory" / project_key(world.root)


def test_computing_the_directory_creates_nothing(world):
    before = snapshot(world.home)
    memory_dir(world.root, world.home)
    assert snapshot(world.home) == before


# ---------------------------------------------------------------------------
# loading the index
# ---------------------------------------------------------------------------
def index_path(world):
    return memory_dir(world.root, world.home) / "MEMORY.md"


def test_no_memory_yet_gives_an_empty_index(world):
    result = load_memory_index(world.root, world.home)
    assert result.text == "" and result.truncated is False and result.notes == ()


def test_a_short_index_is_returned_as_written(world):
    put(index_path(world), "- run tests with pytest -q\n- ports: api on 8000\n")
    result = load_memory_index(world.root, world.home)
    assert result.text == "- run tests with pytest -q\n- ports: api on 8000" and not result.truncated


def test_only_the_first_200_lines_are_returned(world):
    put(index_path(world), "\n".join(f"note {i:04d}" for i in range(500)))
    result = load_memory_index(world.root, world.home)
    lines = result.text.split("\n")
    assert len(lines) == 200 and lines[0] == "note 0000" and lines[-1] == "note 0199"
    assert result.truncated and "truncated" in [n.code for n in result.notes]


def test_exactly_200_lines_is_not_truncated(world):
    put(index_path(world), "\n".join(f"n{i}" for i in range(200)) + "\n")
    result = load_memory_index(world.root, world.home)
    assert len(result.text.split("\n")) == 200 and not result.truncated


def test_only_the_first_25_kb_are_returned(world):
    put(index_path(world), "\n".join(f"{i:03d} " + "m" * 996 for i in range(100)))      # 100 lines of 1000 B
    result = load_memory_index(world.root, world.home)
    assert len(result.text.encode()) <= 25 * 1024
    assert result.text.endswith("m" * 996) and result.truncated           # ends on a whole line
    assert len(result.text.split("\n")) == 25


def test_the_smaller_of_the_two_limits_wins(world):
    put(index_path(world), "\n".join("x" * 5000 for _ in range(10)))      # 10 lines, 50 KB: bytes bind first
    short_lines = load_memory_index(world.root, world.home)
    assert len(short_lines.text.encode()) <= mem.MAX_INDEX_BYTES and len(short_lines.text.split("\n")) == 5
    put(index_path(world), "\n".join("y" for _ in range(1000)))           # 1000 short lines: lines bind first
    many_lines = load_memory_index(world.root, world.home)
    assert len(many_lines.text.split("\n")) == 200


def test_exactly_25_kb_is_not_truncated(world):
    put(index_path(world), "z" * (25 * 1024))
    result = load_memory_index(world.root, world.home)
    assert len(result.text.encode()) == 25 * 1024 and not result.truncated


def test_the_byte_limit_is_in_bytes_not_characters(world):
    put(index_path(world), "\n".join("é" * 1000 for _ in range(40)))      # 2 bytes per character
    result = load_memory_index(world.root, world.home)
    assert len(result.text.encode()) <= 25 * 1024 and "�" not in result.text


def test_invalid_utf8_cannot_push_the_result_over_the_limit(world):
    put(index_path(world), data=b"\xff" * (60 * 1024))                     # each bad byte becomes U+FFFD
    result = load_memory_index(world.root, world.home)
    assert len(result.text.encode()) <= 25 * 1024


def test_the_defaults_are_the_documented_ones():
    assert (mem.MAX_INDEX_LINES, mem.MAX_INDEX_BYTES) == (200, 25 * 1024)


def test_an_index_that_is_a_symlink_out_of_the_memory_directory_is_not_read(world):
    put(world.outside / "private.md", "OUTSIDE-SECRET")
    index_path(world).parent.mkdir(parents=True)
    index_path(world).symlink_to(world.outside / "private.md")
    result = load_memory_index(world.root, world.home)
    assert result.text == "" and "symlink_escape" in [n.code for n in result.notes]


def test_an_index_with_nul_bytes_is_not_sent(world):
    put(index_path(world), data=b"abc\x00def")
    result = load_memory_index(world.root, world.home)
    assert result.text == "" and "not_text" in [n.code for n in result.notes]


def test_a_directory_named_memory_md_is_ignored(world):
    index_path(world).mkdir(parents=True)
    assert load_memory_index(world.root, world.home).text == ""


def test_loading_never_writes_anything(world):
    put(index_path(world), "a note")
    before = snapshot(world.home)
    load_memory_index(world.root, world.home)
    assert snapshot(world.home) == before


def test_memory_of_one_project_is_not_visible_to_another(world, tmp_path):
    put(index_path(world), "PROJECT-ONE-NOTE")
    other = tmp_path / "other"
    other.mkdir()
    assert load_memory_index(other, world.home).text == ""


# ---------------------------------------------------------------------------
# preparing and applying a write
# ---------------------------------------------------------------------------
def test_preparing_a_write_touches_the_disk_not_at_all(world):
    before = snapshot(world.home)
    prepared = prepare_memory_write(world.root, "- the build needs node 20", home=world.home)
    assert isinstance(prepared, PreparedMemoryWrite)
    assert snapshot(world.home) == before and not (world.home / "memory").exists()


def test_the_prepared_write_carries_what_the_user_needs_to_approve(world):
    prepared = prepare_memory_write(world.root, "- the build needs node 20", home=world.home)
    assert prepared.target == "MEMORY.md" and prepared.mode == "append"
    assert "the build needs node 20" in prepared.preview and "MEMORY.md" in prepared.preview
    assert str(world.home) not in prepared.preview


def test_applying_requires_an_explicit_approval(world):
    prepared = prepare_memory_write(world.root, "- note", home=world.home)
    for not_approved in (False, None, 0, 1, "yes", "true", object()):
        with pytest.raises(MemoryWriteNotApproved):
            apply_memory_write(prepared, approved=not_approved)
    assert not (world.home / "memory").exists()


def test_applying_after_approval_writes_the_note(world):
    prepared = prepare_memory_write(world.root, "- the build needs node 20", home=world.home)
    apply_memory_write(prepared, approved=True)
    assert index_path(world).read_text() == "- the build needs node 20\n"
    assert load_memory_index(world.root, world.home).text == "- the build needs node 20"


def test_append_adds_to_the_existing_file_on_its_own_line(world):
    put(index_path(world), "- first")
    apply_memory_write(prepare_memory_write(world.root, "- second", home=world.home), approved=True)
    assert index_path(world).read_text() == "- first\n- second\n"


def test_replace_overwrites_the_file(world):
    put(index_path(world), "- old")
    prepared = prepare_memory_write(world.root, "- brand new", home=world.home, mode="replace")
    assert "replace" in prepared.preview.lower()
    apply_memory_write(prepared, approved=True)
    assert index_path(world).read_text() == "- brand new\n"


def test_a_topic_file_can_be_the_target(world):
    prepared = prepare_memory_write(world.root, "details", home=world.home, target="testing.md")
    apply_memory_write(prepared, approved=True)
    assert (memory_dir(world.root, world.home) / "testing.md").read_text() == "details\n"
    assert not index_path(world).exists()


@pytest.mark.parametrize("target", ["../x.md", "a/b.md", "/etc/x.md", ".hidden.md", "notes.txt", "", "x" * 80 + ".md", "a\nb.md", "..md"])
def test_unsafe_targets_are_refused(world, target):
    with pytest.raises(ValueError):
        prepare_memory_write(world.root, "text", home=world.home, target=target)


def test_an_unknown_mode_is_refused(world):
    with pytest.raises(ValueError):
        prepare_memory_write(world.root, "text", home=world.home, mode="delete")


@pytest.mark.parametrize("text", ["", "   \n\t", "a\x00b"])
def test_empty_or_binary_text_is_refused(world, text):
    with pytest.raises(ValueError):
        prepare_memory_write(world.root, text, home=world.home)


def test_one_write_cannot_be_larger_than_the_cap(world):
    with pytest.raises(ValueError):
        prepare_memory_write(world.root, "n" * (mem.MAX_WRITE_BYTES + 1), home=world.home)
    prepare_memory_write(world.root, "n" * mem.MAX_WRITE_BYTES, home=world.home)


def test_a_file_that_changed_after_preparing_makes_the_apply_fail_closed(world):
    put(index_path(world), "- original")
    prepared = prepare_memory_write(world.root, "- appended", home=world.home)
    index_path(world).write_text("- changed behind our back")
    with pytest.raises(MemoryWriteConflict):
        apply_memory_write(prepared, approved=True)
    assert index_path(world).read_text() == "- changed behind our back"


def test_a_file_created_after_preparing_is_also_a_conflict(world):
    prepared = prepare_memory_write(world.root, "- mine", home=world.home)
    put(index_path(world), "- someone else got there first")
    with pytest.raises(MemoryWriteConflict):
        apply_memory_write(prepared, approved=True)


def test_the_written_files_are_private_to_the_user(world):
    apply_memory_write(prepare_memory_write(world.root, "- note", home=world.home), approved=True)
    assert stat.S_IMODE(index_path(world).stat().st_mode) == 0o600
    assert stat.S_IMODE(index_path(world).parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((world.home / "memory").stat().st_mode) == 0o700


def test_no_temporary_files_are_left_behind(world):
    apply_memory_write(prepare_memory_write(world.root, "- note", home=world.home), approved=True)
    assert sorted(p.name for p in index_path(world).parent.iterdir()) == ["MEMORY.md"]


def test_a_symlinked_memory_file_is_never_written_through(world):
    put(world.outside / "victim.md", "ORIGINAL")
    index_path(world).parent.mkdir(parents=True)
    index_path(world).symlink_to(world.outside / "victim.md")
    with pytest.raises((MemoryWriteConflict, MemoryWriteRefused, ValueError, OSError)):
        prepared = prepare_memory_write(world.root, "- sneaky", home=world.home)   # refusing here is fine ...
        apply_memory_write(prepared, approved=True)                                  # ... and so is refusing here
    assert (world.outside / "victim.md").read_text() == "ORIGINAL"


def test_a_hand_built_prepared_write_cannot_escape_the_memory_directory(world):
    forged = PreparedMemoryWrite(project_key=project_key(world.root), target="../../escape.md", mode="append",
                                 text="x", home=str(world.home), base_sha256=None)
    with pytest.raises(ValueError):
        apply_memory_write(forged, approved=True)
    assert not (world.home / "escape.md").exists() and not (world.home.parent / "escape.md").exists()


def test_a_hand_built_prepared_write_with_a_secret_is_refused_at_apply_time(world):
    forged = PreparedMemoryWrite(project_key=project_key(world.root), target="MEMORY.md", mode="append",
                                 text=ch.fake_github_token(), home=str(world.home), base_sha256=None)
    with pytest.raises(MemoryWriteRefused):
        apply_memory_write(forged, approved=True)
    assert not index_path(world).exists()


def test_a_hand_built_prepared_write_with_a_bad_project_key_is_refused(world):
    forged = PreparedMemoryWrite(project_key="../../../etc", target="MEMORY.md", mode="append",
                                 text="x", home=str(world.home), base_sha256=None)
    with pytest.raises(ValueError):
        apply_memory_write(forged, approved=True)


def test_nothing_is_logged_about_the_content(world):
    import io
    import logging

    from m2c import logging_setup

    stream = io.StringIO()
    logging_setup.configure_logging(logging.DEBUG, stream)
    try:
        prepared = prepare_memory_write(world.root, "UNIQUE-MEMORY-TEXT-8675309", home=world.home)
        apply_memory_write(prepared, approved=True)
        load_memory_index(world.root, world.home)
    finally:
        logging.getLogger("m2c").handlers.clear()
    assert "UNIQUE-MEMORY-TEXT" not in stream.getvalue() and str(world.home) not in stream.getvalue()


# ---------------------------------------------------------------------------
# secrets: refused, with the reason, and the secret itself is never echoed
# ---------------------------------------------------------------------------
SECRET_CASES = [
    ("private_key", ch.fake_private_key_block),
    ("github_token", ch.fake_github_token),
    ("github_token", ch.fake_github_fine_grained),
    ("api_key", ch.fake_sk_key),
    ("aws_access_key", ch.fake_aws_key_id),
    ("slack_token", ch.fake_slack_token),
    ("bearer_token", lambda: ch._cat("curl -H 'Authorization: Bearer ", ch.fake_bearer_value(), "' https://example.com")),
    ("bearer_token", lambda: ch._cat("send Bearer ", ch.fake_bearer_value())),
    ("authorization_header", lambda: ch._cat("Authorization: Basic ", ch._rep("dXNlcjpwYXNz", 2))),
    ("authorization_header", lambda: ch._cat("authorization = ", ch.fake_bearer_value())),
    ("password_assignment", ch.fake_password_line),
    ("password_assignment", lambda: ch._cat("DB_", "PASSWORD=", "hunter2")),
    ("password_assignment", lambda: ch._cat("passwd: ", "'s3cret'")),
    ("secret_assignment", lambda: ch._cat("api_", "key = ", "Zx81Qw92Er73Ty64")),
    ("secret_assignment", lambda: ch._cat("client_", "secret: ", "9f8e7d6c5b4a39281716")),
    ("env_line", ch.fake_env_lines),
    ("env_line", lambda: ch._cat("export ", "SERVICE_HOST=internal.example")),
    ("url_credentials", ch.fake_url_with_credentials),
    ("jwt", ch.fake_jwt),
    ("google_api_key", ch.fake_google_key),
    ("stripe_key", ch.fake_stripe_key),
    ("gitlab_token", ch.fake_gitlab_token),
]


@pytest.mark.parametrize("kind, build", SECRET_CASES, ids=[f"{k}-{i}" for i, (k, _) in enumerate(SECRET_CASES)])
def test_secrets_are_refused_with_a_reason_and_nothing_is_written(world, kind, build):
    secret = build()
    text = "- harmless note\n" + secret + "\n- another harmless note"
    with pytest.raises(MemoryWriteRefused) as caught:
        prepare_memory_write(world.root, text, home=world.home)
    err = caught.value
    assert kind in [f.kind for f in err.findings]
    assert err.findings[0].line == 2
    shown = str(err)
    assert kind.replace("_", " ") in shown or "secret" in shown.lower()
    for piece in (secret, secret[8:30]):
        assert piece not in shown and piece not in repr(err.findings)
    assert not (world.home / "memory").exists()


@pytest.mark.parametrize("kind, build", SECRET_CASES, ids=[f"{k}-{i}" for i, (k, _) in enumerate(SECRET_CASES)])
def test_find_secrets_reports_kind_and_line_only(kind, build):
    findings = find_secrets("line one\n" + build())
    assert kind in [f.kind for f in findings]
    assert all(f.line >= 2 for f in findings) and all(set(vars(f)) == {"kind", "line"} for f in findings)


def test_a_secret_hidden_with_zero_width_characters_is_still_caught():
    token = ch.fake_github_token()
    sneaky = token[:3] + "\u200b" + token[3:12] + "\u2060" + token[12:]
    assert "github_token" in [f.kind for f in find_secrets(sneaky)]


def test_a_secret_in_fullwidth_letters_is_still_caught():
    key = ch.fake_sk_key()
    fullwidth = "".join(chr(ord(c) + 0xFEE0) if "!" <= c <= "~" else c for c in key)
    assert "api_key" in [f.kind for f in find_secrets(fullwidth)]


BENIGN = [
    "- run the tests with `pytest -q` from the m2c folder",
    "The staging deploy uses blue/green switching.",
    "Prefer ruff over flake8; line length is 100.",
    "API keys live in the secrets manager, never in git.",
    "Authorization is handled by the gateway, not the service.",
    "The password reset flow lives in auth/reset.py.",
    "Set `PYTHONPATH=src` before running the scripts.",
    "Bearer tokens are validated in the middleware.",
    "token refresh logic is in auth/session.py",
    "Use sk-learn style naming? No: this repo uses scikit-learn.",
    "The login page is at /login and the API at https://example.com/api/v1/users.",
    "Contact: someone@example.com about the release process.",
    "ports: api on 8000, db on 5432",
    "## Architecture\n\nThe worker pulls jobs from the queue.",
]


@pytest.mark.parametrize("line", BENIGN)
def test_ordinary_notes_are_not_mistaken_for_secrets(world, line):
    assert find_secrets(line) == []
    prepare_memory_write(world.root, line, home=world.home)


def test_the_refusal_names_every_kind_found_and_tells_the_user_what_to_do(world):
    text = ch.fake_github_token() + "\n" + ch.fake_password_line()
    with pytest.raises(MemoryWriteRefused) as caught:
        prepare_memory_write(world.root, text, home=world.home)
    msg = str(caught.value)
    assert "github" in msg.lower() and "password" in msg.lower()
    assert "line 1" in msg and "line 2" in msg
    assert "describe" in msg.lower()           # tells the user to describe, not store, the value


def test_the_module_says_that_c15_generalises_secret_detection():
    assert "C15" in (mem.__doc__ or "") and "C15" in (find_secrets.__doc__ or "")


def test_a_refused_write_cannot_be_forced_through_a_flag(world):
    import inspect

    assert "force" not in inspect.signature(prepare_memory_write).parameters
    assert "force" not in inspect.signature(apply_memory_write).parameters
