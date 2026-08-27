"""
minime_cli — Patch A6: a genuine terminal client for MiniMe.

Talks to the *existing* backend session/messaging infrastructure over
plain HTTP, the same way frontend/app/context/SessionContext.jsx does
(POST /api/chats, POST /api/chats/{id}/messages, POST /api/task) — this
is not a new protocol, and it does not touch daemon/ or backend/eo/
local_workspace*.py at all (see Section 0.1 of the implementation
guide: that machinery is for *local-machine* file/exec access, which
is an orthogonal concern from "a terminal instead of a browser talks
to the chat API").

Package layout:
    config.py            -- resolves API base URL + Supabase project
                             coordinates from env / ~/.minime/config.json
    auth.py               -- login/refresh/logout, credential cache
    api_client.py         -- thin HTTP wrapper mirroring
                              SessionContext.jsx's fetch() call sites
    render.py             -- client-side mirror of api/task_runner.py's
                              _extract_answer_text(), for terminal output
    commands/             -- one module per subcommand group
    main.py               -- click group wiring it all together
"""

__version__ = "0.1.0"
