"""
m2c/tests/secret_handling/test_secrets_runner.py -- Patch C15: redaction at the outbound boundary of the runner.

THE SCRIPTED SESSION
--------------------
Canary secrets (fake, built at run time) are planted in project files and in the environment; a scripted session
runs through C24's real ``ToolRunner`` (real tools, real shell, real policy) against the fake link, which checks every
payload against the protocol schemas.  The assertion that matters: no canary (nor any 8-character window of one)
appears in ANY ``tool_result`` output, ``tool_stream`` chunk or edited-call line the link was handed.

THE OTHER HALF: THE TERMINAL
----------------------------
What the person sees locally is not redacted.  The runner's executor is wrapped by a tap that records what the tools
really produced, and what ``on_chunk`` was given, BEFORE the boundary: those still hold the canaries.  (In this version
the person's terminal shows tool activity from the runner's own callbacks and the server's run events, never from the
outgoing copy; the redaction happens only on the copy that leaves.)
"""
from __future__ import annotations

import dataclasses
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

_RUNNER_TESTS = str(Path(__file__).resolve().parents[1] / "runner")
if _RUNNER_TESTS not in sys.path:
    sys.path.insert(0, _RUNNER_TESTS)

import pytest  # noqa: E402
from runner_rig import MARKER, capture_runner_logs, make_layers, make_rig, run  # noqa: E402
from secret_fakes import leaked, make

import m2c.secrets.redact as redact_mod
from m2c.policy import Context
from m2c.runner import EDITED_NOTICE, Choice, ToolRunner
from m2c.secrets.redact import FAIL_CLOSED_TEXT
from m2c.store import open_db
from m2c.tools import ToolContext, run_tool


class Tap:
    """An executor that runs the real tools and records what they produced, and what ``on_chunk`` saw, untouched."""

    def __init__(self) -> None:
        self.raw_results: list[str] = []
        self.raw_chunks: list[str] = []
        self.raw_inputs: list = []

    def __call__(self, context, tool, args, *, call_env=None):
        self.raw_inputs.append(args)
        if call_env is not None and call_env.on_chunk is not None:
            downstream = call_env.on_chunk

            def tee(stream, text):
                self.raw_chunks.append(text)
                downstream(stream, text)

            call_env = dataclasses.replace(call_env, on_chunk=tee)
        result = run_tool(context, tool, args, call_env=call_env)
        self.raw_results.append(result.output)
        return result


def write_grants(home, project_root, names):
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.toml").write_text(
        f'[environment.projects.{json.dumps(str(project_root))}]\nkeep = {json.dumps(list(names))}\n')


def plant(rig, monkeypatch, tmp_path):
    """Plant canaries in files and in the environment.  Returns {label: secret}."""
    canaries = {
        "env_file_token": make("secret_assignment_env", 21),
        "env_file_aws": make("aws_secret_key", 22),
        "notes_github": make("github_token", 23),
        "notes_jwt": make("jwt", 24),
        "key_block": make("private_key_block", 25),
        "deploy_url": make("url_credentials", 26),
        "env_openai": make("openai_style_key", 27),
        "env_jwt": make("jwt", 28),
        "edited_command": make("github_token", 29),
    }
    root = rig.root
    (root / ".env").write_text("\n".join([canaries["env_file_token"].text, canaries["env_file_aws"].text, "DEBUG=1"]) + "\n")
    (root / "notes").mkdir()
    (root / "notes" / "deploy.md").write_text(
        "# Deploy\n" + canaries["notes_github"].text + "\n" + canaries["notes_jwt"].text + "\n" + canaries["deploy_url"].text + "\n")
    (root / "keys").mkdir()
    (root / "keys" / "id_test").write_text(canaries["key_block"].text + "\n")
    # the policy refuses read_file and ``cat`` on ".env" (C06), so those calls use plain file names; ``grep -rn .`` reads .env
    (root / "config").mkdir()
    (root / "config" / "settings.txt").write_text((root / ".env").read_text() + canaries["key_block"].text + "\n")
    monkeypatch.setenv("OPENAI_API_KEY", canaries["env_openai"].secret)
    monkeypatch.setenv("BUILD_NOTE_JWT", canaries["env_jwt"].secret)
    write_grants(tmp_path / "m2c-home", root, ["OPENAI_API_KEY", "BUILD_NOTE_JWT"])
    return canaries


def everything_sent(link) -> list[str]:
    texts = []
    for kind, payload in link.sent:
        if kind == "tool_result":
            texts.append(payload["output"])
        elif kind == "tool_stream":
            texts.append(payload["chunk"])
    return texts


def test_no_canary_in_a_file_or_the_environment_reaches_the_link(tmp_path, monkeypatch):
    tap = Tap()
    answers = {"n": 0}

    def ask(request):
        answers["n"] += 1
        return Choice("allow", "once")

    rig = make_rig(tmp_path, ask_fn=ask, executor=tap)
    canaries = plant(rig, monkeypatch, tmp_path)

    async def scenario():
        await rig.call("c1", "read_file", {"path": "config/settings.txt"})
        await rig.call("c2", "read_file", {"path": "notes/deploy.md"})
        await rig.call("c3", "bash", {"command": "cat keys/id_test"}, "R2")
        await rig.call("c4", "grep", {"pattern": "."})
        await rig.call("c5", "bash", {"command": "cat config/settings.txt notes/deploy.md keys/id_test"}, "R2")
        await rig.call("c6", "bash", {"command": 'echo "$OPENAI_API_KEY"; echo "$BUILD_NOTE_JWT"; env | sort'}, "R2")
        await rig.call("c7", "bash", {"command": "grep -rn . notes keys .env"}, "R2")

    run(scenario())

    sent = everything_sent(rig.link)
    assert any(kind == "tool_stream" for kind, _ in rig.link.sent), "the scripted session should have streamed"
    for label, fake in canaries.items():
        if label == "edited_command":
            continue
        for text in sent:
            assert not leaked(text, fake.secret), f"canary {label} reached the link"
    assert all("[REDACTED:" in rig.link.result(c)["output"] for c in ("c1", "c2", "c3", "c5"))

    # the person's side: the tools really did produce the canaries, and on_chunk saw them raw
    raw_results = "\n".join(tap.raw_results)
    raw_chunks = "".join(tap.raw_chunks)
    for label in ("env_file_token", "notes_github", "key_block", "env_openai", "env_jwt"):
        assert canaries[label].secret in raw_results, f"{label}: the raw tool output should still hold it"
    assert canaries["env_openai"].secret in raw_chunks and canaries["notes_github"].secret in raw_chunks


def test_a_changed_result_carries_a_short_fixed_note_with_kinds_and_counts_only(tmp_path, monkeypatch):
    rig = make_rig(tmp_path)
    fake = make("github_token", 3)
    (rig.root / "a.txt").write_text(fake.text + "\n" + fake.text + "\n")
    (rig.root / "b.txt").write_text("nothing secret in here\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    run(rig.call("c2", "read_file", {"path": "b.txt"}))
    out = rig.link.result("c1")["output"]
    assert "github_token x2" in out.splitlines()[-1] and "redacted" in out.splitlines()[-1].lower()
    assert not leaked(out, fake.secret)
    clean = rig.link.result("c2")["output"]
    assert "redacted" not in clean.lower() and "nothing secret in here" in clean


def test_the_edited_call_line_is_redacted_too_and_the_edited_command_still_ran_as_edited(tmp_path):
    tap = Tap()
    fake = make("github_token", 31)
    edited = {"command": f"echo using {fake.secret} now"}
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "once", edited_input=edited), executor=tap)
    run(rig.call("c1", "bash", {"command": "echo original"}, "R2"))
    out = rig.link.result("c1")["output"]
    assert out.startswith(EDITED_NOTICE) and not leaked(out, fake.secret)
    assert "[REDACTED:github_token]" in out.split("\n\n", 1)[0]
    assert tap.raw_inputs[-1] == edited                      # what ran is what the person typed, untouched
    assert not any(leaked(text, fake.secret) for text in everything_sent(rig.link))


def test_a_non_shell_edited_call_line_is_redacted_too(tmp_path):
    fake = make("jwt", 33)
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice(
        "allow", edited_input={"path": "b.txt", "content": "token " + fake.secret + "\n"}))
    run(rig.call("c1", "write_file", {"path": "a.txt", "content": "x\n"}, "R1"))
    assert not leaked(rig.link.result("c1")["output"], fake.secret)
    assert (rig.root / "b.txt").read_text() == "token " + fake.secret + "\n"      # the file itself is the person's


def test_stream_chunks_are_redacted_even_when_the_secret_is_split_across_output_writes(tmp_path):
    fake = make("github_token", 41)
    half = len(fake.secret) // 2
    cmd = f"printf '%s' '{fake.secret[:half]}'; sleep 0.3; printf '%s\\n' '{fake.secret[half:]}'; echo done"
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "once"))
    run(rig.call("c1", "bash", {"command": cmd}, "R2"))
    chunks = "".join(p["chunk"] for p in rig.link.payloads("tool_stream"))
    assert "done" in chunks and not leaked(chunks, fake.secret) and "[REDACTED:github_token]" in chunks
    assert not leaked(rig.link.result("c1")["output"], fake.secret)


def test_stdout_and_stderr_are_redacted_independently(tmp_path):
    a, b = make("github_token", 51), make("jwt", 52)
    cmd = f"echo {a.secret}; echo {b.secret} >&2; echo ok"
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "once"))
    run(rig.call("c1", "bash", {"command": cmd}, "R2"))
    streams = {p["stream"] for p in rig.link.payloads("tool_stream")}
    text = "".join(p["chunk"] for p in rig.link.payloads("tool_stream"))
    assert streams <= {"stdout", "stderr"} and not leaked(text, a.secret) and not leaked(text, b.secret)


def test_the_headless_path_returns_redacted_output_too(tmp_path):
    fake = make("aws_access_key", 61)
    project = tmp_path / "hl"
    project.mkdir()
    (project / "creds.txt").write_text(fake.text + "\n")
    layers = make_layers(allow=["read_file(*)"])
    runner = ToolRunner(None, run_id=None, tool_context=ToolContext(project),
                        policy_context=lambda: Context(mode="ask", layers=layers, project_root=str(project), project_trusted=True),
                        store=open_db(), replay_settle_s=0.0)
    proposal = SimpleNamespace(call_id="h1", tool="read_file", input={"path": "creds.txt"}, risk="R0")
    outcome = run(runner.execute(proposal))
    assert outcome.ok and "[REDACTED:aws_access_key]" in outcome.output and not leaked(outcome.output, fake.secret)


def test_redaction_that_grows_the_text_still_respects_the_wire_limit(tmp_path):
    from m2c.tools import MAX_WIRE_CHARS

    rig = make_rig(tmp_path)
    tokens = [make("npm_token", i).secret for i in range(200)]
    (rig.root / "many.txt").write_text(("\n".join(tokens) + "\n") * 40)
    run(rig.call("c1", "read_file", {"path": "many.txt"}))
    result = rig.link.result("c1")
    assert len(result["output"]) <= MAX_WIRE_CHARS and not any(leaked(result["output"], t) for t in tokens[:5])


# ---- fail closed --------------------------------------------------------------------------------------------------
def test_a_redaction_error_withholds_the_result_but_keeps_the_status(tmp_path, monkeypatch):
    rig = make_rig(tmp_path)
    (rig.root / "a.txt").write_text("harmless looking text\n")

    def boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(redact_mod, "_scan", boom)
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    result = rig.link.result("c1")
    assert result["output"] == FAIL_CLOSED_TEXT and result["ok"] is True and "harmless" not in result["output"]


def test_a_redaction_error_withholds_stream_chunks_too(tmp_path, monkeypatch):
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "once"))

    def boom(*args, **kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(redact_mod, "_scan", boom)
    run(rig.call("c1", "bash", {"command": "echo visible-text-1234; sleep 0.2; echo more"}, "R2"))
    chunks = "".join(p["chunk"] for p in rig.link.payloads("tool_stream"))
    assert "visible-text" not in chunks and rig.link.result("c1")["output"] == FAIL_CLOSED_TEXT


# ---- logging: kinds and counts only --------------------------------------------------------------------------------
def test_nothing_secret_nor_any_output_is_logged_only_kinds_and_counts(tmp_path, monkeypatch, caplog):
    logs = capture_runner_logs(monkeypatch)
    caplog.set_level(logging.DEBUG)                  # also clears the logger's level cache, as the C24 audit test does
    fake = make("github_token", 71)
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "once"))
    (rig.root / "a.txt").write_text(fake.text + "\n" + MARKER + "\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    run(rig.call("c2", "bash", {"command": f"echo {fake.secret}"}, "R2"))
    assert "runner.redacted" in logs.text and "code=github_token" in logs.text and "count=1" in logs.text
    assert fake.secret[:12] not in logs.text and MARKER not in logs.text and fake.text not in logs.text


# ---- the negative control: the assertions above can fail ----------------------------------------------------------
def test_without_the_redaction_the_canaries_would_reach_the_link(tmp_path, monkeypatch):
    """If this passed with redaction switched off, the canary tests above would prove nothing."""
    import m2c.runner as runner_mod

    class PassThrough:
        findings = ()
        failed = False

        def feed(self, text):
            return text

        def flush(self):
            return ""

    monkeypatch.setattr(runner_mod.ToolRunner, "_protect", lambda self, call, outcome: outcome)
    monkeypatch.setattr(runner_mod, "StreamRedactor", PassThrough)
    fake = make("github_token", 81)
    rig = make_rig(tmp_path, ask_fn=lambda request: Choice("allow", "once"))
    (rig.root / "a.txt").write_text(fake.text + "\n")
    run(rig.call("c1", "read_file", {"path": "a.txt"}))
    run(rig.call("c2", "bash", {"command": f"echo {fake.secret}; sleep 0.2; echo end"}, "R2"))
    assert leaked(rig.link.result("c1")["output"], fake.secret)
    assert leaked("".join(p["chunk"] for p in rig.link.payloads("tool_stream")), fake.secret)
