"""
m2c/src/m2c/tui/__init__.py -- the terminal chat (C09).

Patch C09a added the PURE half: everything that decides what is shown or what a key means, with no terminal I/O, so
the terminal library can be swapped and every decision can be tested with plain strings.  Patch C09b adds the rest:
``session`` (the controller) and ``app`` (the thin terminal layer).

    safe      untrusted text made safe to print (escape sequences, control and format characters, markup)
    diff      a unified diff of a proposed edit, collapsed when large
    render    a server event -> display blocks (streaming text, tool cards, notices, errors, reply_restarted)
    approval  the approval prompt as a state machine (y a n d e v), and the per-session memory
    status    the status line, the context estimate, and the live mode cycle (never into plan)
    keys      Esc versus Esc Esc
    history   the conversation rebuilt from the local log for ``run.start`` (honors rewind markers; size-bounded)
    complete  ``@`` file mentions (denylist, no symlinks, bounded scan)
    grant     ``g``: ask the server for more credits for a run paused for budget

    session   the controller: the link, the tool runner, the log, runs with history, rewind, interrupt, approvals, grants
    app       the ``UI`` protocol and the prompt_toolkit/rich layer (the only module that imports them; cli.py imports it
              lazily, so headless mode never loads them)

The pure modules and ``session`` import neither prompt_toolkit nor rich.
"""
