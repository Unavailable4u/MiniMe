"""
minime_cli/main.py -- console-script entry point (`minime`, see
cli/pyproject.toml's [project.scripts]).

Wires the command modules together. Kept deliberately thin: every
command's actual logic lives in commands/*.py so this file stays a
pure dispatch table, easy to extend in A7 (`minime attach`) and A8
(`minime skills ...`, `minime mcp ...`) without touching this file's
existing commands.
"""
from __future__ import annotations

import click

from . import __version__
from .commands.attach_cmds import attach
from .commands.auth_cmds import configure, login, logout, whoami
from .commands.chat_cmds import ask, chat, list_chats_cmd


@click.group()
@click.version_option(__version__, prog_name="minime")
def cli():
    """MiniMe CLI -- talk to your MiniMe backend from the terminal."""


cli.add_command(configure)
cli.add_command(login)
cli.add_command(logout)
cli.add_command(whoami)
cli.add_command(ask)
cli.add_command(chat)
cli.add_command(list_chats_cmd)
cli.add_command(attach)


def main():
    cli()


if __name__ == "__main__":
    main()
