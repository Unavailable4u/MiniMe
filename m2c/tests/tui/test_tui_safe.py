"""Patch C09a: untrusted text is made safe before it is shown (m2c.tui.safe)."""
import random

import pytest

from m2c.tui.safe import clean, clean_line, escape_markup, safe_name, truncate

HOSTILE = [
    "\x1b[31mred\x1b[0m",                       # color
    "a\x1b[2J\x1b[Hb",                          # clear screen, cursor home
    "x\x1b]0;owned\x07y",                       # set window title (BEL)
    "x\x1b]0;owned\x1b\\y",                     # set window title (ST)
    "x\x1b]8;;http://evil.example\x1b\\link\x1b]8;;\x1b\\y",   # hyperlink
    "p\x1bPqpayload\x1b\\q",                    # DCS
    "k\x9b31mm\x9d0;t\x07z",                    # 8-bit CSI and OSC
    "line\rovewrite",                            # carriage return
    "tro\u202ejan",                              # bidi override
    "zero\u200bwidth\ufeff",                     # zero width
    "bell\x07 back\x08 nul\x00 del\x7f",
    "unterminated \x1b]0;never ends",
    "\ud800 lone surrogate",
]


@pytest.mark.parametrize("text", HOSTILE)
def test_clean_removes_everything_a_terminal_could_act_on(text):
    out = clean(text)
    for ch in out:
        assert ch in "\n\t" or ch.isprintable(), repr(ch)
    assert "\x1b" not in out and "\x9b" not in out and "\x9d" not in out


def test_clean_keeps_ordinary_text_newlines_and_tabs():
    assert clean("héllo\tworld\nline two ✓ 日本語") == "héllo\tworld\nline two ✓ 日本語"


def test_carriage_returns_become_newlines_not_overwrites():
    assert clean("a\r\nb\rc") == "a\nb\nc"


def test_clean_of_a_non_string_is_empty():
    for value in (None, 5, b"x", ["a"], {"a": 1}):
        assert clean(value) == ""


def test_title_and_hyperlink_sequences_leave_no_payload_behind():
    assert clean("x\x1b]0;owned\x07y") == "xy"
    assert clean("x\x1b]8;;http://evil.example\x1b\\link\x1b]8;;\x1b\\y") == "xlinky"


def test_a_sequence_split_across_two_pieces_leaves_only_harmless_letters():
    pieces = ["red \x1b[3", "1mtext"]
    assert "\x1b" not in "".join(clean(p) for p in pieces)


def test_clean_line_collapses_whitespace_and_cuts():
    assert clean_line("a\n\n  b\t c") == "a b c"
    out = clean_line("x" * 500, 20)
    assert len(out) == 20 and out.endswith("…")


def test_truncate_edges():
    assert truncate("abc", 0) == "" and truncate("abc", 3) == "abc" and truncate("abcd", 3) == "ab…"


def test_safe_name_accepts_only_a_safe_alphabet():
    assert safe_name("edit_file") == "edit_file" and safe_name("mcp-server_tool") == "mcp-server_tool"
    for bad in ("[red]x", "a b", "\x1b[0m", "", "9lives", "x" * 65, None, 5):
        assert safe_name(bad) == "unknown"


def test_markup_is_escaped_so_rich_renders_it_literally():
    rich_markup = pytest.importorskip("rich.markup")
    for text in ("[bold red]hi[/]", "[link=http://x]a[/link]", "[/]", "plain", "a [red on blue] b", "[[x]]"):
        rendered = rich_markup.render(escape_markup(text))
        assert rendered.plain == clean(text) and not rendered.spans


def test_markup_escape_never_lets_a_style_through_even_for_odd_backslashes():
    rich_markup = pytest.importorskip("rich.markup")
    rng = random.Random(7)
    pieces = list("[]\\/abcbold=#@ ") + ["[bold]", "[/bold]", "[link=x]", "[/]", "[red on blue]"]
    for _ in range(3000):
        text = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 9)))
        rendered = rich_markup.render(escape_markup(text))
        assert not rendered.spans
        if "\\" not in text:
            assert rendered.plain == clean(text)
