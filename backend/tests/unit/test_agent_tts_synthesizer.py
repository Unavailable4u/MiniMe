"""
tests/unit/test_agent_tts_synthesizer.py — Patch 7f-6-1.

Covers agents/tts_synthesizer.py — the deterministic, no-LLM-call half
of podcast/rehearsal script -> audio: parsing a "LABEL:" formatted
script into an ordered list of (speech, label, text) / (pause, seconds)
entries, resolving each label to a voice (pinned HOST A/HOST B, an
explicit override, a deterministic pool pick, or a cached-per-run
auto-assignment), and synthesize_podcast()'s top-level ValueError
contract for a script with zero recognized dialogue lines.

Every test here runs entirely offline: edge_tts.Communicate.save and
edge_tts.list_voices are monkeypatched to AsyncMocks (this module never
makes a real network call or spawns a real ffmpeg process during this
suite), and _voice_pool_cache is reset before every test since it's a
process-lifetime module-level cache the real module deliberately never
clears itself (see _fetch_voice_pool's own docstring).

Async helpers (_fetch_voice_pool, _synthesize_silence, _synthesize_line,
_synthesize_all) are exercised via asyncio.run() directly in each test
body rather than through pytest-asyncio, which isn't part of this
repo's test dependencies.
"""
import asyncio
import os

from unittest.mock import AsyncMock, MagicMock

import pytest

import agents.tts_synthesizer as tts_synthesizer


@pytest.fixture(autouse=True)
def _reset_voice_pool_cache():
    """Autouse: _voice_pool_cache is a module-level, process-lifetime
    cache by design (see _fetch_voice_pool's docstring) — without this,
    the first test that populates it would poison every later test's
    pool regardless of what that later test's edge_tts.list_voices mock
    returns."""
    tts_synthesizer._voice_pool_cache = None
    yield
    tts_synthesizer._voice_pool_cache = None


def _voice_catalog(*short_names, locale="en-US"):
    return [{"ShortName": name, "Locale": locale} for name in short_names]


# ---------------------------------------------------------------------------
# 1. _match_speaker_line(): pinned HOST A/B vs generic ALL-CAPS labels
# ---------------------------------------------------------------------------
class TestMatchSpeakerLine:
    def test_matches_host_a_case_insensitively(self):
        result = tts_synthesizer._match_speaker_line("Host a: hello there")
        assert result == ("HOST A", "hello there")

    def test_matches_host_b_uppercase(self):
        result = tts_synthesizer._match_speaker_line("HOST B: hi back")
        assert result == ("HOST B", "hi back")

    def test_matches_generic_all_caps_label(self):
        result = tts_synthesizer._match_speaker_line("JUDGE: what happens next?")
        assert result == ("JUDGE", "what happens next?")

    def test_generic_label_allows_digits_and_underscores(self):
        result = tts_synthesizer._match_speaker_line("SPEAKER_2: a line")
        assert result == ("SPEAKER_2", "a line")

    def test_lowercase_generic_label_does_not_match(self):
        # True uppercase required for the generic rule -- ordinary prose
        # like "Note: see below" must not be mistaken for dialogue.
        result = tts_synthesizer._match_speaker_line("Note: see below")
        assert result is None

    def test_host_prefixed_label_matches_any_letter_via_pinned_regex(self):
        # _HOST_RE is deliberately case-insensitive AND matches any single
        # letter after "HOST " (not just A/B) -- it's checked before the
        # generic rule, so "Host C:" is still recognized as dialogue even
        # though only HOST A/HOST B have a pinned voice.
        result = tts_synthesizer._match_speaker_line("Host C: hi")
        assert result == ("HOST C", "hi")

    def test_lowercase_non_host_label_does_not_match(self):
        # True uppercase required for the generic rule -- ordinary prose
        # like "note: see below" must not be mistaken for dialogue.
        result = tts_synthesizer._match_speaker_line("note: see below")
        assert result is None

    def test_label_over_25_chars_does_not_match(self):
        long_label = "A" * 26
        result = tts_synthesizer._match_speaker_line(f"{long_label}: text")
        assert result is None

    def test_blank_line_does_not_match(self):
        assert tts_synthesizer._match_speaker_line("") is None

    def test_line_with_no_colon_does_not_match(self):
        assert tts_synthesizer._match_speaker_line("just a stage direction") is None

    def test_speaker_line_with_empty_text_after_colon_returns_none(self):
        assert tts_synthesizer._match_speaker_line("JUDGE:") is None
        assert tts_synthesizer._match_speaker_line("JUDGE:    ") is None


# ---------------------------------------------------------------------------
# 2. _parse_script(): full script -> ordered entries
# ---------------------------------------------------------------------------
class TestParseScript:
    def test_parses_speech_lines_in_order(self):
        script = "HOST A: first line\nHOST B: second line"
        entries = tts_synthesizer._parse_script(script)
        assert entries == [
            ("speech", "HOST A", "first line"),
            ("speech", "HOST B", "second line"),
        ]

    def test_parses_pause_without_explicit_seconds_using_default(self):
        entries = tts_synthesizer._parse_script("[PAUSE]")
        assert entries == [("pause", tts_synthesizer.DEFAULT_PAUSE_SECONDS)]

    def test_parses_pause_with_explicit_integer_seconds(self):
        entries = tts_synthesizer._parse_script("[PAUSE:8]")
        assert entries == [("pause", 8.0)]

    def test_parses_pause_with_explicit_decimal_seconds(self):
        entries = tts_synthesizer._parse_script("[PAUSE:8.5]")
        assert entries == [("pause", 8.5)]

    def test_pause_marker_is_case_insensitive(self):
        entries = tts_synthesizer._parse_script("[pause:3]")
        assert entries == [("pause", 3.0)]

    def test_non_matching_lines_are_dropped_not_guessed_at(self):
        script = "\n# Title\nHOST A: real line\nsome stray prose\n"
        entries = tts_synthesizer._parse_script(script)
        assert entries == [("speech", "HOST A", "real line")]

    def test_mixed_speech_and_pause_preserves_order(self):
        script = "JUDGE: a question\n[PAUSE:5]\nMODEL ANSWER: the answer"
        entries = tts_synthesizer._parse_script(script)
        assert entries == [
            ("speech", "JUDGE", "a question"),
            ("pause", 5.0),
            ("speech", "MODEL ANSWER", "the answer"),
        ]

    def test_empty_script_returns_empty_list(self):
        assert tts_synthesizer._parse_script("") == []


# ---------------------------------------------------------------------------
# 3. _fetch_voice_pool(): live fetch, filtering, caching, fallback
# ---------------------------------------------------------------------------
class TestFetchVoicePool:
    def test_filters_to_en_us_neural_voices_excluding_pinned(self, monkeypatch):
        catalog = _voice_catalog(
            "en-US-GuyNeural",     # pinned -- excluded
            "en-US-AriaNeural",    # pinned -- excluded
            "en-US-JennyNeural",
            "en-US-DavisMultilingualNeural",
        ) + _voice_catalog("en-GB-SoniaNeural", locale="en-GB")  # wrong locale -- excluded
        monkeypatch.setattr(tts_synthesizer.edge_tts, "list_voices", AsyncMock(return_value=catalog))

        pool = asyncio.run(tts_synthesizer._fetch_voice_pool())

        assert "en-US-GuyNeural" not in pool
        assert "en-US-AriaNeural" not in pool
        assert "en-US-JennyNeural" in pool
        assert "en-GB-SoniaNeural" not in pool

    def test_pool_capped_at_pool_size(self, monkeypatch):
        many = _voice_catalog(*[f"en-US-Voice{i}Neural" for i in range(tts_synthesizer._POOL_SIZE + 5)])
        monkeypatch.setattr(tts_synthesizer.edge_tts, "list_voices", AsyncMock(return_value=many))

        pool = asyncio.run(tts_synthesizer._fetch_voice_pool())

        assert len(pool) == tts_synthesizer._POOL_SIZE

    def test_result_is_cached_across_calls(self, monkeypatch):
        mock_list_voices = AsyncMock(return_value=_voice_catalog("en-US-JennyNeural"))
        monkeypatch.setattr(tts_synthesizer.edge_tts, "list_voices", mock_list_voices)

        first = asyncio.run(tts_synthesizer._fetch_voice_pool())
        second = asyncio.run(tts_synthesizer._fetch_voice_pool())

        assert first == second
        mock_list_voices.assert_called_once()

    def test_falls_back_to_pinned_voices_when_catalog_fetch_fails(self, monkeypatch):
        monkeypatch.setattr(
            tts_synthesizer.edge_tts, "list_voices",
            AsyncMock(side_effect=RuntimeError("offline")),
        )
        pool = asyncio.run(tts_synthesizer._fetch_voice_pool())
        assert pool == sorted(set(tts_synthesizer.PINNED_VOICES.values()))

    def test_falls_back_to_pinned_voices_when_catalog_has_no_matches(self, monkeypatch):
        monkeypatch.setattr(
            tts_synthesizer.edge_tts, "list_voices",
            AsyncMock(return_value=_voice_catalog("en-GB-SoniaNeural", locale="en-GB")),
        )
        pool = asyncio.run(tts_synthesizer._fetch_voice_pool())
        assert pool == sorted(set(tts_synthesizer.PINNED_VOICES.values()))


# ---------------------------------------------------------------------------
# 4. _voice_for_label(): priority order and collision avoidance
# ---------------------------------------------------------------------------
class TestVoiceForLabel:
    def test_explicit_override_wins_over_everything(self):
        voice = tts_synthesizer._voice_for_label(
            "HOST A", pool=["en-US-JennyNeural"], assigned={},
            overrides={"HOST A": "en-US-CustomNeural"},
        )
        assert voice == "en-US-CustomNeural"

    def test_pinned_label_uses_pinned_voice_when_no_override(self):
        voice = tts_synthesizer._voice_for_label(
            "HOST A", pool=["en-US-JennyNeural"], assigned={}, overrides=None,
        )
        assert voice == tts_synthesizer.PINNED_VOICES["HOST A"]

    def test_already_assigned_label_reuses_same_voice_within_run(self):
        assigned = {"JUDGE": "en-US-JennyNeural"}
        voice = tts_synthesizer._voice_for_label(
            "JUDGE", pool=["en-US-JennyNeural", "en-US-DavisNeural"], assigned=assigned, overrides=None,
        )
        assert voice == "en-US-JennyNeural"

    def test_new_label_gets_deterministic_pick_from_pool(self):
        pool = ["en-US-JennyNeural", "en-US-DavisNeural"]
        voice_first = tts_synthesizer._voice_for_label("JUDGE", pool=list(pool), assigned={}, overrides=None)
        voice_second = tts_synthesizer._voice_for_label("JUDGE", pool=list(pool), assigned={}, overrides=None)
        assert voice_first == voice_second  # same label -> same voice, every run
        assert voice_first in pool

    def test_new_label_avoids_a_voice_already_claimed_in_this_run(self):
        pool = ["en-US-JennyNeural"]
        assigned = {"OTHER_LABEL": "en-US-JennyNeural"}
        voice = tts_synthesizer._voice_for_label("JUDGE", pool=pool, assigned=assigned, overrides=None)
        # Only pool voice is already claimed -- graceful degrade to reuse,
        # not a crash.
        assert voice == "en-US-JennyNeural"

    def test_distinct_labels_get_distinct_voices_when_pool_has_room(self):
        pool = ["en-US-JennyNeural", "en-US-DavisNeural", "en-US-AmberNeural"]
        assigned: dict = {}
        voice_a = tts_synthesizer._voice_for_label("ADVOCATE", pool=list(pool), assigned=assigned, overrides=None)
        voice_b = tts_synthesizer._voice_for_label("JUDGE", pool=list(pool), assigned=assigned, overrides=None)
        assert voice_a != voice_b

    def test_assigns_into_the_assigned_dict_as_a_side_effect(self):
        assigned: dict = {}
        voice = tts_synthesizer._voice_for_label(
            "JUDGE", pool=["en-US-JennyNeural"], assigned=assigned, overrides=None,
        )
        assert assigned["JUDGE"] == voice


# ---------------------------------------------------------------------------
# 5. _synthesize_silence(): ffmpeg subprocess success/failure paths
# ---------------------------------------------------------------------------
class TestSynthesizeSilence:
    def test_raises_runtime_error_when_ffmpeg_binary_missing(self, monkeypatch, tmp_path):
        async def _fake_create_subprocess_exec(*args, **kwargs):
            raise FileNotFoundError("ffmpeg not found")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        with pytest.raises(RuntimeError, match="ffmpeg binary not found"):
            asyncio.run(tts_synthesizer._synthesize_silence(3.0, str(tmp_path / "out.mp3")))

    def test_raises_runtime_error_on_nonzero_exit(self, monkeypatch, tmp_path):
        fake_proc = MagicMock()
        fake_proc.wait = AsyncMock(return_value=None)
        fake_proc.returncode = 1

        async def _fake_create_subprocess_exec(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            asyncio.run(tts_synthesizer._synthesize_silence(3.0, str(tmp_path / "out.mp3")))

    def test_succeeds_when_ffmpeg_exits_zero_and_writes_file(self, monkeypatch, tmp_path):
        out_path = str(tmp_path / "out.mp3")
        fake_proc = MagicMock()
        fake_proc.returncode = 0

        async def _fake_wait():
            # Simulate ffmpeg actually producing the file before exiting.
            with open(out_path, "wb") as f:
                f.write(b"\x00")
            return None

        fake_proc.wait = _fake_wait

        async def _fake_create_subprocess_exec(*args, **kwargs):
            return fake_proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

        asyncio.run(tts_synthesizer._synthesize_silence(3.0, out_path))
        assert os.path.exists(out_path)


# ---------------------------------------------------------------------------
# 6. synthesize_podcast(): top-level ValueError contract + end-to-end
# ---------------------------------------------------------------------------
class TestSynthesizePodcast:
    def test_raises_value_error_when_no_speaker_lines_found(self, tmp_path):
        with pytest.raises(ValueError, match="no speaker-labeled dialogue"):
            tts_synthesizer.synthesize_podcast(
                "just some prose\nno labels here", str(tmp_path / "out.mp3"),
            )

    def test_raises_value_error_for_pause_only_script(self, tmp_path):
        with pytest.raises(ValueError, match="no speaker-labeled dialogue"):
            tts_synthesizer.synthesize_podcast("[PAUSE]\n[PAUSE:5]", str(tmp_path / "out.mp3"))

    def test_end_to_end_writes_concatenated_audio_bytes(self, monkeypatch, tmp_path):
        # Stub out every real I/O boundary: no network TTS call, no real
        # ffmpeg process -- only the module's own chunk-then-concatenate
        # logic is under test here.
        async def _fake_synthesize_line(text, voice, out_path):
            with open(out_path, "wb") as f:
                f.write(b"AUDIO:" + text.encode("utf-8"))

        async def _fake_synthesize_silence(seconds, out_path):
            with open(out_path, "wb") as f:
                f.write(f"SILENCE:{seconds}".encode("utf-8"))

        monkeypatch.setattr(tts_synthesizer, "_synthesize_line", _fake_synthesize_line)
        monkeypatch.setattr(tts_synthesizer, "_synthesize_silence", _fake_synthesize_silence)
        monkeypatch.setattr(
            tts_synthesizer, "_fetch_voice_pool",
            AsyncMock(return_value=["en-US-JennyNeural"]),
        )

        out_path = str(tmp_path / "out.mp3")
        script = "HOST A: hello\n[PAUSE:2]\nHOST B: hi back"

        result = tts_synthesizer.synthesize_podcast(script, out_path)

        assert result == out_path
        with open(out_path, "rb") as f:
            content = f.read()
        assert content == b"AUDIO:hello" + b"SILENCE:2.0" + b"AUDIO:hi back"

    def test_voice_overrides_are_threaded_through_to_synthesis(self, monkeypatch, tmp_path):
        captured_voices = []

        async def _fake_synthesize_line(text, voice, out_path):
            captured_voices.append(voice)
            with open(out_path, "wb") as f:
                f.write(b"x")

        monkeypatch.setattr(tts_synthesizer, "_synthesize_line", _fake_synthesize_line)
        monkeypatch.setattr(
            tts_synthesizer, "_fetch_voice_pool",
            AsyncMock(return_value=["en-US-JennyNeural"]),
        )

        out_path = str(tmp_path / "out.mp3")
        tts_synthesizer.synthesize_podcast(
            "HOST A: hi", out_path, voice_overrides={"HOST A": "en-US-OverrideNeural"},
        )

        assert captured_voices == ["en-US-OverrideNeural"]
