"""
m2c/src/m2c/sandbox/bridge.py -- Patch C11b: the bridge that gives a sandboxed command a proxy to talk to.

WHAT THIS FILE IS
-----------------
C11a runs a shell command in a bubblewrap sandbox whose network namespace is UNSHARED: the command has loopback and
nothing else.  C11b gives it *controlled* network without ever giving it a network:

    command --HTTP CONNECT--> 127.0.0.1:3128 (this bridge, inside the sandbox)
            --unix socket---> proxy on the host (netproxy.py), which decides, resolves, pins and connects.

The bridge is deliberately dumb.  It relays bytes between a TCP connection on the sandbox's own loopback and the unix
socket that the host proxy listens on.  It decides nothing, reads nothing and logs nothing; every decision is made by
the proxy on the host, so a command that talks to the unix socket directly, or kills the bridge and starts its own,
gains nothing.  A program that ignores the proxy settings simply cannot connect: there is no route (fail closed by
construction).

TWO ROLES IN ONE FILE
---------------------
* As a MODULE (imported by m2c): the constants, ``NetworkMount``, ``proxy_environment`` / ``apply_proxy_environment``
  (the variables injected into the command's environment AFTER sanitizing) and ``launcher_argv``.
* As a SCRIPT (copied by the proxy into its private directory, mounted read-only into the sandbox and run by the
  system ``python3`` there):  ``python3 -I bridge.py --port P --sock PATH -- COMMAND...``.  It binds
  127.0.0.1:P, forks the relay server into the background and ``exec``s COMMAND, so the command is the process whose
  exit status bubblewrap reports (nothing about exit codes or signals changes).  If the bridge cannot be set up for
  any reason the command is still run: it just has no network, which is the C11a behavior.

The script part uses the standard library only and syntax that runs on Python 3.8 and later, because it runs on the
sandbox's system python, not on m2c's.  It must never import anything from ``m2c``.

Place this file at: m2c/src/m2c/sandbox/bridge.py
"""
from __future__ import annotations

import os
import select
import socket
import sys
import threading
from dataclasses import dataclass
from typing import Dict, List, Mapping, Sequence

__all__ = [
    "BRIDGE_HOST", "BRIDGE_PORT", "NETWORK_DIR_DEST", "SCRIPT_NAME", "SOCKET_NAME", "NetworkMount", "PROXY_VARIABLES",
    "apply_proxy_environment", "bridge_source", "launcher_argv", "main", "proxy_environment", "proxy_url",
]

BRIDGE_HOST = "127.0.0.1"
# Every sandbox has its own network namespace, so a fixed port can never collide with anything on the host.
BRIDGE_PORT = 3128
NETWORK_DIR_DEST = "/run/m2c-net"      # where the proxy's private directory appears inside the sandbox (read-only)
SOCKET_NAME = "proxy.sock"
SCRIPT_NAME = "bridge.py"

MAX_CLIENTS = 256                      # connections the bridge relays at once; the proxy has its own, tighter caps
_CHUNK = 65536
_LISTEN_BACKLOG = 128

# The names the injected environment owns.  Any host variable that differs only by case is removed first.
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


@dataclass(frozen=True)
class NetworkMount:
    """What the wrapper needs to give one sandboxed command a network: the HOST directory that holds the proxy's unix
    socket and the bridge script (mounted read-only at ``NETWORK_DIR_DEST``) and the absolute path of the system
    python3 that runs the bridge inside the sandbox."""

    directory: str
    python: str


def proxy_url() -> str:
    return "http://%s:%d" % (BRIDGE_HOST, BRIDGE_PORT)


def proxy_environment() -> Dict[str, str]:
    """The proxy variables, upper and lower case.  NO_PROXY is EMPTY on purpose: nothing is exempt from the proxy,
    so no inherited ``NO_PROXY`` can send a tool around it (and there is nowhere to go around it to).  Note for
    users: a command's own ``http://localhost:PORT`` test server is therefore also reached through the proxy, which
    refuses loopback; that is the price of an empty NO_PROXY."""
    url = proxy_url()
    env = {}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        env[name] = url
        env[name.lower()] = url
    env["NO_PROXY"] = ""
    env["no_proxy"] = ""
    return env


def apply_proxy_environment(env: Mapping[str, str]) -> Dict[str, str]:
    """A copy of ``env`` with the proxy variables set to the bridge.  Call it on the SANITIZED environment: any
    variable whose upper-cased name is one of ``PROXY_VARIABLES`` (the host's own proxy settings, which the sanitizer
    may or may not have kept, in any letter case) is dropped first, then ours are set."""
    out = {k: v for k, v in env.items() if k.upper() not in PROXY_VARIABLES}
    out.update(proxy_environment())
    return out


def launcher_argv(python: str, shell: str, command: str) -> List[str]:
    """The program and arguments that follow ``bwrap ... --`` for a command with network: the system python runs the
    bridge script from the mounted directory, which then execs ``shell -c command``."""
    return [
        python, "-I", NETWORK_DIR_DEST + "/" + SCRIPT_NAME,
        "--port", str(BRIDGE_PORT), "--sock", NETWORK_DIR_DEST + "/" + SOCKET_NAME,
        "--", shell, "-c", command,
    ]


def bridge_source() -> str:
    """The text of this file; the proxy writes it into its private directory.  Read from the module's own source so
    that what runs in the sandbox is exactly what the tests exercised."""
    with open(__file__, "r", encoding="utf-8") as handle:
        return handle.read()


# ==========================================================================
# the script part: runs inside the sandbox, on its python
# ==========================================================================
def _relay(client, sock_path):
    """Relay one connection: bytes both ways until both directions have ended.  A half-close is passed on."""
    upstream = None
    try:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.settimeout(10)
        upstream.connect(sock_path)
        upstream.settimeout(None)
        client.settimeout(None)
        pairs = {client: upstream, upstream: client}
        open_sources = [client, upstream]
        while open_sources:
            readable, _, _ = select.select(open_sources, [], [], 600)
            if not readable:
                break                                  # idle for ten minutes: the proxy has its own idle limit too
            for source in readable:
                try:
                    data = source.recv(_CHUNK)
                except OSError:
                    data = b""
                target = pairs[source]
                if data:
                    try:
                        target.sendall(data)
                    except OSError:
                        return
                else:
                    open_sources.remove(source)
                    try:
                        target.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
    except Exception:  # noqa: BLE001 - a relay failure ends this connection only
        pass
    finally:
        for s in (client, upstream):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


def serve(listener, sock_path):
    """Accept connections forever; one thread relays each.  Never returns (the process is killed with the sandbox)."""
    slots = threading.BoundedSemaphore(MAX_CLIENTS)

    def run(conn):
        try:
            _relay(conn, sock_path)
        finally:
            slots.release()

    while True:
        try:
            conn, _ = listener.accept()
        except OSError:
            continue
        if not slots.acquire(False):
            conn.close()
            continue
        thread = threading.Thread(target=run, args=(conn,))
        thread.daemon = True
        thread.start()


def _parse(argv):
    """``--port N --sock PATH -- COMMAND...`` -> (port, sock, command) or None when it is not that."""
    port = sock = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            command = list(argv[i + 1:])
            if port is None or sock is None or not command:
                return None
            return port, sock, command
        if arg == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                return None
            i += 2
        elif arg == "--sock" and i + 1 < len(argv):
            sock = argv[i + 1]
            i += 2
        else:
            return None
    return None


def main(argv: Sequence[str] = ()) -> int:
    """Bind, fork the relay into the background, exec the command.  Returns only when the exec cannot happen."""
    parsed = _parse(list(argv))
    if parsed is None:
        sys.stderr.write("m2c bridge: bad arguments\n")
        return 2
    port, sock_path, command = parsed
    listener = None
    try:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((BRIDGE_HOST, port))
        listener.listen(_LISTEN_BACKLOG)
        pid = os.fork()
        if pid == 0:                                   # the relay server: detached from the command's pipes
            try:
                devnull = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(devnull, fd)
                serve(listener, sock_path)
            finally:
                os._exit(0)
        listener.close()                               # the parent keeps nothing open across the exec
    except Exception:  # noqa: BLE001 - no bridge means no network, never a failed command
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
    try:
        os.execv(command[0], command)
    except OSError as exc:
        sys.stderr.write("m2c bridge: cannot run the command (%s)\n" % exc.__class__.__name__)
        return 127


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
