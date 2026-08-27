"""
minime_cli/commands/chat_cmds.py -- `minime ask`, `minime chat`, `minime chats`.

Persistence discipline mirrors SessionContext.jsx's sendTask() exactly:
the user's turn is persisted BEFORE the task is dispatched, and the
assistant's turn is persisted right after the response comes back --
so a chat started from the CLI shows up in the web UI's sidebar with a
correct, complete transcript, not just a final answer with no visible
question.
"""
from __future__ import annotations

import click

from .. import auth
from ..api_client import ApiClient, ApiError
from ..config import ConfigError, load_config
from ..render import extract_answer_text, format_status_line


def _client() -> ApiClient:
    return ApiClient(load_config())


def _run_turn(client: ApiClient, chat_id: str, task_text: str) -> dict:
    """One user turn -> one assistant turn, persisted on both sides.
    Shared by `ask` and `chat`'s per-line loop so the two commands
    can't drift in behavior."""
    client.persist_message(chat_id, {"role": "user", "text": task_text})
    data = client.send_task(task_text, session_id=chat_id)
    # Trimmed relative to _buildAssistantMessage() in SessionContext.jsx:
    # this drops the Working Panel fields (steps/routeTrace/roleRequests/
    # dependencyMap/structurePlan) since those exist to feed a live UI
    # panel the CLI doesn't have one of -- `data` (the full TaskResponse)
    # is kept in full, so nothing about the actual answer is lost, and
    # the web UI reading this same chat back still gets a `data` field
    # in the shape it expects.
    assistant_message = {"role": "assistant", "data": data, "task": task_text}
    client.persist_message(chat_id, assistant_message)
    return data


def _print_response(data: dict) -> None:
    status_line = format_status_line(data)
    if status_line:
        click.echo(click.style(status_line, fg="yellow"), err=True)
    click.echo(extract_answer_text(data))


@click.command()
@click.argument("task_text")
@click.option("--chat", "chat_id", default=None, help="Continue an existing chat_id instead of starting a new one.")
@click.option("--title", default=None, help="Title for a newly created chat (ignored if --chat is given).")
def ask(task_text, chat_id, title):
    """One-shot: send TASK_TEXT and print the answer.

    Starts a new chat unless --chat CHAT_ID is given. Prints the new
    chat's id to stderr on creation so a follow-up `minime ask --chat
    <id> "..."` can continue the same conversation.
    """
    try:
        client = _client()
        if chat_id is None:
            chat = client.create_chat(title=title or task_text[:60])
            chat_id = chat["id"]
            click.echo(f"(new chat: {chat_id})", err=True)
        data = _run_turn(client, chat_id, task_text)
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))
    _print_response(data)


@click.command()
@click.option("--chat", "chat_id", default=None, help="Continue an existing chat_id instead of starting a new one.")
@click.option("--title", default=None, help="Title for a newly created chat (ignored if --chat is given).")
def chat(chat_id, title):
    """Interactive loop: type a message, get an answer, repeat.

    Exactly `ask`, run in a loop against one persistent chat_id, with a
    prompt_toolkit-free plain input() -- this is meant to be usable
    over a bare SSH session with no extra terminal capability assumed.
    """
    try:
        client = _client()
        if chat_id is None:
            chat_obj = client.create_chat(title=title or "CLI chat")
            chat_id = chat_obj["id"]
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))

    click.echo(f"Chatting in {chat_id}. Ctrl-D or /exit to quit.")
    while True:
        try:
            line = click.prompt("you", prompt_suffix="> ")
        except (EOFError, click.exceptions.Abort):
            click.echo()
            break
        if line.strip() in ("/exit", "/quit"):
            break
        if not line.strip():
            continue
        try:
            data = _run_turn(client, chat_id, line)
        except (auth.AuthError, ApiError) as e:
            # A single failed turn shouldn't kill the whole session --
            # the user's message is already persisted above (or, if
            # persist_message itself is what failed, at least the loop
            # can keep going for the next turn) -- print and continue.
            click.echo(click.style(f"error: {e}", fg="red"), err=True)
            continue
        _print_response(data)


@click.command(name="chats")
def list_chats_cmd():
    """List your chats (id, title)."""
    try:
        chats = _client().list_chats()
    except (ConfigError, auth.AuthError, ApiError) as e:
        raise click.ClickException(str(e))
    if not chats:
        click.echo("No chats yet -- `minime ask \"...\"` to start one.")
        return
    for c in chats:
        click.echo(f"{c.get('id')}\t{c.get('title', 'New Chat')}")
