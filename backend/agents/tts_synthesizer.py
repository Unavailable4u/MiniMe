"""
agents/tts_synthesizer.py — Part 4 §4.4. Deterministic, no-LLM-call audio
synthesis for podcast_scriptwriter's (and now rehearsal_scriptwriter's)
output. edge-tts is free, makes local network calls to Microsoft's public
endpoint, needs no API key -- matches every other Capture/Generate tool
agent's no-paid-process constraint (Part 4 §4.8's cost accounting already
assumes this for the synthesis half).

Script generation (the reasoning half) is podcast_scriptwriter /
rehearsal_scriptwriter, plain generic_worker roles -- see their briefs in
eo/registry.py's ROLE_LIBRARY. This module is synthesis only: it never
calls an LLM, it just reads a script's "LABEL:" formatted Markdown and
turns it into one audio file. Same reasoning-vs-deterministic-tool split
every other Generate step in this domain already follows.

CHANGED — Notebooks Chat-First Refinement, Phase 5 step 5.9 finding:
presentation_rehearsal needs more than two fixed voices (a single JUDGE,
a single ADVOCATE, a MODEL ANSWER line, possibly a future 3+-person
roundtable) and a way to insert silence for the user to answer into. The
old version of this module hardcoded exactly "HOST A"/"HOST B" in both
the parser regex and a 2-entry VOICE_MAP -- every new persona would have
meant another hardcoded regex branch and another hardcoded voice, a
ceiling this engine's "versatile, not a fixed number of works" posture
explicitly doesn't want. This version instead:

  1. Parses ANY short, ALL-CAPS "LABEL:" line as a speaker turn (not just
     the literal string "HOST [A-Z]") -- a role brief's only obligation
     is to use short, consistent, ALL-CAPS labels, exactly what
     podcast_scriptwriter's brief already promised before this change.
  2. Keeps HOST A -> en-US-GuyNeural / HOST B -> en-US-AriaNeural PINNED
     exactly as before (byte-for-byte identical behavior for every
     existing caller -- notebooks_podcast, notebooks_video_overview, and
     the legacy synthesize_podcast_endpoint all only ever emit those two
     labels today).
  3. Any OTHER label (JUDGE, ADVOCATE, MODEL ANSWER, or literally
     anything a future scriptwriter role decides to emit) gets a voice
     auto-assigned, deterministically (same label -> same voice, every
     run), from a pool fetched live from edge_tts.list_voices() the
     first time it's needed -- never a hardcoded, potentially-stale list
     of voice IDs. Falls back to the two pinned voices if the catalog
     fetch fails (offline dev box, etc.) rather than crashing generation.
  4. Adds a first-class "[PAUSE]" / "[PAUSE:8]" line type, rendered as a
     silent mp3 chunk via a direct ffmpeg call (ffmpeg is already a
     required binary in this stack -- see agents/video_overview_builder.py's
     own docstring -- so this adds no new dependency, and existing
     podcast/video_overview scripts that never use "[PAUSE]" never
     invoke this code path at all).

synthesize_podcast()'s call signature is unchanged for every existing
caller (script_text, out_path) -- voice_overrides is new and optional,
defaulted to None, purely for a future caller that wants to pin a
specific label to a specific voice on purpose.

Place this file at: agents/tts_synthesizer.py
"""
import os
import re
import asyncio
import hashlib
import tempfile

import edge_tts

# Pinned first, unchanged from the original two-host-only version -- every
# script emitted by podcast_scriptwriter today only ever uses these two
# labels, so this alone is what keeps notebooks_podcast/
# notebooks_video_overview/synthesize_podcast_endpoint byte-for-byte
# identical after this change.
PINNED_VOICES = {
    "HOST A": "en-US-GuyNeural",
    "HOST B": "en-US-AriaNeural",
}

# Old literal "HOST [A-Z]" regex, kept verbatim (including its
# case-insensitivity) so a script that happens to say "Host A:" instead
# of "HOST A:" still parses exactly as it always has. Checked BEFORE the
# generic rule below, so this exact-backward-compat path always wins for
# these two labels regardless of case.
_HOST_RE = re.compile(r"^\s*(HOST [A-Z])\s*:\s*(.+)$", re.IGNORECASE)

# NEW — generic rule: any short, ALL-CAPS label (letters/digits/space/
# underscore, <=25 chars) followed by a colon is a speaker turn. Requires
# true uppercase (not case-insensitive) so ordinary prose ("Note: see
# below") can't be mistaken for a speaker line -- a role brief that wants
# a line recognized as dialogue has to use a real label, same discipline
# podcast_scriptwriter's brief already asks for.
_GENERIC_SPEAKER_RE = re.compile(r"^\s*([A-Z][A-Z0-9 _]{0,24})\s*:\s*(.+)$")

# "[PAUSE]" (uses DEFAULT_PAUSE_SECONDS) or "[PAUSE:8]" / "[PAUSE:8.5]"
# (explicit seconds) -- a script's way of asking for a silent gap, e.g.
# rehearsal_scriptwriter's judge mode leaving room for the user to answer
# out loud before the model answer plays.
_PAUSE_RE = re.compile(r"^\s*\[\s*PAUSE(?:\s*:\s*(\d+(?:\.\d+)?))?\s*\]\s*$", re.IGNORECASE)
DEFAULT_PAUSE_SECONDS = 5.0

# How many auto-assigned (non-pinned) voices to keep in the pool once
# fetched -- plenty of headroom for any realistic number of simultaneous
# personas in one script without making every list_voices() call pull
# and sort the entire multi-hundred-voice catalog into memory for no
# reason.
_POOL_SIZE = 8

_voice_pool_cache: list[str] | None = None


def _match_speaker_line(raw_line: str) -> tuple[str, str] | None:
    """Returns (label, text) for a recognized speaker line, or None.
    Checks the pinned HOST A/HOST B pattern first (exact legacy
    behavior), then falls back to the generic ALL-CAPS rule for any
    other label."""
    match = _HOST_RE.match(raw_line)
    if match:
        text = match.group(2).strip()
        return (match.group(1).upper(), text) if text else None

    match = _GENERIC_SPEAKER_RE.match(raw_line)
    if match:
        text = match.group(2).strip()
        return (match.group(1).strip(), text) if text else None

    return None


def _parse_script(script_text: str) -> list[tuple]:
    """Splits script_text into an ordered list of entries, each either
    ("speech", label, text) or ("pause", seconds). A line matching
    neither a speaker label nor a "[PAUSE]" marker (a blank line, a
    stage direction, a stray title) is dropped rather than guessed at --
    same "a silently mis-attributed line is worse than a silently
    dropped non-dialogue one" reasoning the original parser already
    used."""
    entries: list[tuple] = []
    for raw_line in script_text.splitlines():
        pause_match = _PAUSE_RE.match(raw_line)
        if pause_match:
            seconds = float(pause_match.group(1)) if pause_match.group(1) else DEFAULT_PAUSE_SECONDS
            entries.append(("pause", seconds))
            continue

        spoken = _match_speaker_line(raw_line)
        if spoken:
            label, text = spoken
            entries.append(("speech", label, text))

    return entries


async def _fetch_voice_pool() -> list[str]:
    """Live catalog fetch, cached for the process lifetime -- deliberately
    NOT a hardcoded list of voice IDs in source, since edge-tts's catalog
    can and does drift (voices get added/renamed/retired). Filters to
    en-US neural voices not already pinned, sorted for determinism, capped
    at _POOL_SIZE. Falls back to the two pinned voices (so auto-assignment
    degrades to "reuse a pinned voice" rather than crashing generation)
    if the catalog fetch itself fails -- e.g. an offline dev box, or the
    same kind of network restriction this sandbox environment hit while
    designing this module."""
    global _voice_pool_cache
    if _voice_pool_cache is not None:
        return _voice_pool_cache

    try:
        catalog = await edge_tts.list_voices()
        pinned = set(PINNED_VOICES.values())
        candidates = sorted(
            v["ShortName"] for v in catalog
            if v.get("Locale") == "en-US"
            and "Neural" in v.get("ShortName", "")
            and v["ShortName"] not in pinned
        )
        _voice_pool_cache = candidates[:_POOL_SIZE] or sorted(pinned)
    except Exception:
        _voice_pool_cache = sorted(set(PINNED_VOICES.values()))

    return _voice_pool_cache


def _voice_for_label(
    label: str,
    pool: list[str],
    assigned: dict[str, str],
    overrides: dict[str, str] | None,
) -> str:
    """Resolves label -> voice for one synthesis run, in priority order:
    an explicit per-call override, the pinned HOST A/HOST B map, a voice
    already auto-assigned to this label earlier in the SAME script, or a
    fresh deterministic pick from the pool. The deterministic pick skips
    any voice already claimed by another label in this run (pinned or
    auto), so two distinct personas in one script never end up sounding
    identical purely by hash collision -- falls back to allowing reuse
    only if every pool voice is already claimed (more simultaneous
    personas than pool size), which is a graceful degrade, not a
    crash."""
    if overrides and label in overrides:
        return overrides[label]
    if label in PINNED_VOICES:
        return PINNED_VOICES[label]
    if label in assigned:
        return assigned[label]

    used = set(assigned.values()) | set(PINNED_VOICES.values())
    available = [v for v in pool if v not in used] or pool or list(PINNED_VOICES.values())
    index = int(hashlib.md5(label.encode("utf-8")).hexdigest(), 16) % len(available)
    voice = available[index]
    assigned[label] = voice
    return voice


async def _synthesize_line(text: str, voice: str, out_path: str) -> None:
    await edge_tts.Communicate(text, voice).save(out_path)


async def _synthesize_silence(seconds: float, out_path: str) -> None:
    """Generates `seconds` of silent audio at out_path via a direct ffmpeg
    call (anullsrc filter) -- not a new dependency, since ffmpeg is
    already required by agents/video_overview_builder.py. Raises
    RuntimeError with a clear message (missing binary or non-zero exit)
    rather than letting a cryptic subprocess failure surface -- same
    "fail loudly, not mysteriously" posture every other tool agent in
    this domain takes."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono",
            "-t", str(seconds), "-q:a", "9", out_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except FileNotFoundError:
        raise RuntimeError(
            "ffmpeg binary not found -- required for '[PAUSE]' lines "
            "(and already required for Video Overview builds)"
        )
    if proc.returncode != 0 or not os.path.exists(out_path):
        raise RuntimeError(f"ffmpeg failed to generate {seconds}s of silence")


async def _synthesize_all(
    entries: list[tuple],
    out_path: str,
    voice_overrides: dict[str, str] | None,
) -> None:
    """One entry, one chunk (a TTS call for speech, an ffmpeg silence
    render for a pause), in order -- then raw byte concatenation into a
    single mp3, same approach (and same "good enough for a narrated-audio
    use case, not studio-grade mastering" honesty) the original version
    of this module already used for two-voice dialogue."""
    pool = await _fetch_voice_pool()
    assigned: dict[str, str] = {}

    with tempfile.TemporaryDirectory() as tmp_dir:
        chunk_paths = []
        for i, entry in enumerate(entries):
            chunk_path = os.path.join(tmp_dir, f"{i:04d}.mp3")
            if entry[0] == "pause":
                _, seconds = entry
                await _synthesize_silence(seconds, chunk_path)
            else:
                _, label, text = entry
                voice = _voice_for_label(label, pool, assigned, voice_overrides)
                await _synthesize_line(text, voice, chunk_path)
            chunk_paths.append(chunk_path)

        with open(out_path, "wb") as out_file:
            for chunk_path in chunk_paths:
                with open(chunk_path, "rb") as chunk_file:
                    out_file.write(chunk_file.read())


def synthesize_podcast(
    script_text: str,
    out_path: str,
    voice_overrides: dict[str, str] | None = None,
) -> str:
    """Turns a "LABEL:"-formatted Markdown script into one mp3 at
    out_path. Handles any number of distinct speaker labels (not just
    "HOST A"/"HOST B") and "[PAUSE]" silence markers -- see this module's
    docstring for the full design. Raises ValueError if no speaker-labeled
    dialogue lines are found at all -- same one-exception-type contract
    agents/voice_ingestor.py and agents/web_clipper.py already use, so a
    caller can catch ValueError for "bad input" without knowing this
    module's internals. `voice_overrides` is optional and unused by every
    current caller -- see this module's docstring for why it exists.
    """
    entries = _parse_script(script_text)
    if not any(entry[0] == "speech" for entry in entries):
        raise ValueError("no speaker-labeled dialogue lines found in script_text")
    asyncio.run(_synthesize_all(entries, out_path, voice_overrides))
    return out_path


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python tts_synthesizer.py <script.md>")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        result = synthesize_podcast(f.read(), "/tmp/podcast_out.mp3")
    print(f"wrote {result}")
