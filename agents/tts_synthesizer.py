"""
agents/tts_synthesizer.py — Part 4 §4.4. Deterministic, no-LLM-call audio
synthesis for podcast_scriptwriter's output. edge-tts is free, makes local
network calls to Microsoft's public endpoint, needs no API key -- matches
every other Capture/Generate tool agent's no-paid-process constraint (Part
4 §4.8's cost accounting already assumes this for the synthesis half).

Script generation (the reasoning half) is podcast_scriptwriter, a plain
generic_worker role -- see its brief in eo/registry.py's ROLE_LIBRARY. This
module is synthesis only: it never calls an LLM, it just reads
podcast_scriptwriter's "HOST A:"/"HOST B:" formatted Markdown and turns it
into one audio file. Same reasoning-vs-deterministic-tool split every other
Generate step in this domain already follows (slide_planner's JSON gets
rendered by a deterministic exporter, not by another LLM call).

Place this file at: agents/tts_synthesizer.py
"""
import os
import re
import asyncio
import tempfile

import edge_tts

# Two distinct, easily-told-apart voices. Not configurable per-call --
# Capture ingestors are zero-config (voice_ingestor.py's _MODEL_SIZE
# comment), and Generate's synthesis half follows the same rule.
VOICE_MAP = {
    "HOST A": "en-US-GuyNeural",
    "HOST B": "en-US-AriaNeural",
}
DEFAULT_VOICE = "en-US-GuyNeural"

# Matches podcast_scriptwriter's own brief: "give them short consistent
# labels like 'HOST A:'/'HOST B:' at the start of each line". Case-
# insensitive and tolerant of a trailing space before the colon, since an
# LLM's formatting isn't perfectly deterministic even when the brief asks
# for one exact shape.
_LINE_RE = re.compile(r"^\s*(HOST [A-Z])\s*:\s*(.+)$", re.IGNORECASE)


def _parse_script(script_text: str) -> list[tuple[str, str]]:
    """Splits script_text into (speaker_label, line_text) tuples, one per
    spoken line. A line with no HOST X: prefix (blank line, stage
    direction, a stray title) is dropped rather than guessed into a
    speaker -- a silently mis-attributed line is worse than a silently
    dropped non-dialogue one."""
    lines = []
    for raw_line in script_text.splitlines():
        match = _LINE_RE.match(raw_line)
        if not match:
            continue
        text = match.group(2).strip()
        if text:
            lines.append((match.group(1).upper(), text))
    return lines


async def _synthesize_line(text: str, voice: str, out_path: str) -> None:
    await edge_tts.Communicate(text, voice).save(out_path)


async def _synthesize_all(dialogue: list[tuple[str, str]], out_path: str) -> None:
    """One line, one voice call, in order -- then raw byte concatenation
    into a single mp3. mp3 frames concatenate cleanly for playback without
    re-encoding, which is good enough for a narrated-podcast use case. Not
    a claim of studio-grade mastering (no crossfades, no gain matching) --
    same "label the approximation honestly" discipline the notes doc
    applies to Video Overview."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        chunk_paths = []
        for i, (speaker, text) in enumerate(dialogue):
            voice = VOICE_MAP.get(speaker, DEFAULT_VOICE)
            chunk_path = os.path.join(tmp_dir, f"{i:04d}.mp3")
            await _synthesize_line(text, voice, chunk_path)
            chunk_paths.append(chunk_path)

        with open(out_path, "wb") as out_file:
            for chunk_path in chunk_paths:
                with open(chunk_path, "rb") as chunk_file:
                    out_file.write(chunk_file.read())


def synthesize_podcast(script_text: str, out_path: str) -> str:
    """Turns podcast_scriptwriter's Markdown script into one mp3 at
    out_path. Raises ValueError if no HOST X: dialogue lines are found --
    same one-exception-type contract agents/voice_ingestor.py and
    agents/web_clipper.py already use, so a caller can catch ValueError
    for "bad input" without knowing this module's internals.
    """
    dialogue = _parse_script(script_text)
    if not dialogue:
        raise ValueError("no 'HOST A:'/'HOST B:' dialogue lines found in script_text")
    asyncio.run(_synthesize_all(dialogue, out_path))
    return out_path


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python tts_synthesizer.py <script.md>")
        sys.exit(1)
    with open(sys.argv[1]) as f:
        result = synthesize_podcast(f.read(), "/tmp/podcast_out.mp3")
    print(f"wrote {result}")