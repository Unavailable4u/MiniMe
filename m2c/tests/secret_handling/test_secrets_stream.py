"""
m2c/tests/secret_handling/test_secrets_stream.py -- Patch C15: the stateful stream redactor.

A secret split across two (or twenty) chunks must not leak: every kind, at every split position, fed in two pieces
and one character at a time, with and without a closing newline.  The stream result must equal the one-shot result.
"""
from __future__ import annotations

import random

import pytest
from secret_fakes import BUILDERS, leaked, make

import m2c.secrets.redact as redact_mod
from m2c.secrets.redact import FAIL_CLOSED_TEXT, StreamRedactor, redact


def run_stream(chunks, **kwargs) -> tuple[str, StreamRedactor]:
    stream = StreamRedactor(**kwargs)
    out = "".join(stream.feed(chunk) for chunk in chunks)
    return out + stream.flush(), stream


@pytest.mark.parametrize("ending", ["", "\n"])
@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_kind_split_at_every_position_never_leaks(name, ending):
    fake = make(name, 11)
    text = fake.text + ending
    expected = redact(text).text
    for cut in range(len(text) + 1):
        out, stream = run_stream([text[:cut], text[cut:]])
        assert not leaked(out, fake.secret), f"{name}: leaked when split at {cut}"
        assert out == expected, f"{name}: split at {cut} differs from the one-shot result"
        assert not stream.failed


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_every_kind_fed_one_character_at_a_time_never_leaks(name):
    fake = make(name, 12)
    text = "first line\n" + fake.text + "\nlast line\n"
    out, _ = run_stream(list(text))
    assert not leaked(out, fake.secret) and out == redact(text).text


@pytest.mark.parametrize("name", sorted(BUILDERS))
def test_three_way_splits_around_the_secret_never_leak(name):
    fake = make(name, 13)
    text = "x " + fake.text + " y\n"
    rng = random.Random(name)
    for _ in range(40):
        a, b = sorted(rng.sample(range(len(text) + 1), 2))
        out, _ = run_stream([text[:a], text[a:b], text[b:]])
        assert not leaked(out, fake.secret)


def test_random_chunking_of_a_mixed_document_equals_the_one_shot_result():
    rng = random.Random(5)
    names = sorted(BUILDERS)
    lines = []
    for i in range(80):
        lines.append(f"ordinary line {i}: nothing to see")
        if i % 3 == 0:
            lines.append(make(rng.choice(names), i).text)
    document = "\n".join(lines) + "\n"
    expected = redact(document).text
    for trial in range(10):
        cuts = sorted(rng.sample(range(len(document) + 1), rng.randint(1, 60)))
        pieces = [document[a:b] for a, b in zip([0, *cuts], [*cuts, len(document)])]
        out, _ = run_stream(pieces)
        assert out == expected


def test_complete_lines_are_emitted_promptly_and_a_partial_line_is_held_until_it_ends():
    stream = StreamRedactor()
    assert stream.feed("hello\nwor") == "hello\n"
    assert stream.feed("ld\n") == "world\n"
    assert stream.feed("no newline yet") == ""
    assert stream.flush() == "no newline yet"
    assert stream.flush() == ""


def test_carriage_return_progress_updates_are_emitted_as_they_complete():
    stream = StreamRedactor()
    assert stream.feed("10%\r20%\r") == "10%\r20%\r"


def test_findings_accumulate_across_chunks_as_kinds_and_counts():
    fake = make("github_token")
    _, stream = run_stream([fake.text[:9], fake.text[9:] + "\n", fake.text + "\n"])
    assert [(f.kind, f.count) for f in stream.findings] == [("github_token", 2)]


def test_an_unterminated_key_block_is_held_and_redacted_when_the_footer_arrives():
    fake = make("private_key_block")
    head, rest = fake.text.split("\n", 2)[0:2], fake.text
    out, _ = run_stream([fake.text[:40], fake.text[40:90], fake.text[90:]])
    assert not leaked(out, fake.secret) and "[REDACTED:private_key]" in out
    assert out.startswith("before the key\n") and out.endswith("after the key")


def test_an_unterminated_key_block_is_redacted_at_flush():
    fake = make("private_key_block")
    cut = fake.text.split("-----END")[0]
    out, _ = run_stream([cut[:50], cut[50:]])
    assert not leaked(out, fake.secret) and "[REDACTED:private_key]" in out


def test_a_huge_key_block_is_swallowed_with_one_marker_and_bounded_memory():
    fake = make("private_key_block")
    head = fake.text.split("\n")[1]
    foot = fake.text.split("\n")[-2]
    stream = StreamRedactor(max_block=2_000)
    out = stream.feed("before\n" + head + "\n")
    body = "QUJD" * 16 + "\n"
    for _ in range(500):
        out += stream.feed(body)
        assert len(stream._pending) < 5_000
    out += stream.feed(foot + "\nafter\n") + stream.flush()
    assert out == "before\n[REDACTED:private_key]after\n" or out == "before\n[REDACTED:private_key]\nafter\n"
    assert "QUJD" not in out


def test_a_footer_split_across_chunks_still_ends_the_swallow():
    fake = make("private_key_block")
    lines = fake.text.split("\n")
    head, foot = lines[1], lines[-2]
    stream = StreamRedactor(max_block=300)
    out = stream.feed(head + "\n" + ("QUJD" * 16 + "\n") * 20)
    out += stream.feed(foot[:11]) + stream.feed(foot[11:] + "\nafter the key\n") + stream.flush()
    assert "after the key" in out and "QUJD" not in out


def test_a_very_long_line_is_cut_at_whitespace_so_memory_stays_bounded():
    stream = StreamRedactor(max_hold=4_000)
    out = ""
    for _ in range(200):
        out += stream.feed("word " * 100)               # no newline, ever
        assert len(stream._pending) <= 4_000 + 600
    out += stream.flush()
    assert out == "word " * 20_000


def test_a_secret_beyond_a_forced_cut_is_still_redacted_with_the_context_before_it():
    fake = make("password_env")
    secret = fake.secret
    stream = StreamRedactor(max_hold=2_000)
    out = stream.feed("filler " * 400 + _split_name(fake.text, secret)[0])
    out += stream.feed(secret + " " + "more " * 600)
    out += stream.flush()
    assert not leaked(out, secret)


def _split_name(text: str, secret: str) -> tuple[str, str]:
    before, after = text.split(secret)
    return before, after


def test_a_long_run_without_whitespace_is_cut_without_losing_text():
    stream = StreamRedactor(max_hold=2_000)
    blob = "A1b2" * 5_000
    out = stream.feed(blob) + stream.feed(blob) + stream.flush()
    assert out == blob + blob


# ---- fail closed ------------------------------------------------------------------------------------------------
def test_a_stream_error_emits_the_fixed_text_once_and_nothing_after(monkeypatch):
    stream = StreamRedactor()
    assert stream.feed("fine line\n") == "fine line\n"

    def boom(*args, **kwargs):
        raise RuntimeError("broken")

    monkeypatch.setattr(redact_mod, "_scan", boom)
    out = stream.feed("another line\n")
    assert out == FAIL_CLOSED_TEXT and stream.failed
    monkeypatch.undo()
    assert stream.feed("more\n") == "" and stream.flush() == ""


def test_a_non_string_chunk_fails_the_stream_closed():
    stream = StreamRedactor()
    assert stream.feed(b"bytes") == FAIL_CLOSED_TEXT  # type: ignore[arg-type]
    assert stream.feed("later\n") == ""
