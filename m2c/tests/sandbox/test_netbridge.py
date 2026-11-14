"""
m2c/tests/sandbox/test_netbridge.py -- Patch C11b: the bridge (m2c.sandbox.bridge), module part and script part.

The script part is run for real as a subprocess on the host (no sandbox needed): it must bind, relay between a TCP
connection and a unix socket, and then ``exec`` the command so that the command's own exit status and signals are what
the caller sees.  If the bridge cannot be set up the command must STILL run (without network).  The module part builds
the proxy environment: injected after sanitizing, upper and lower case, with an EMPTY NO_PROXY, replacing whatever the
host had.

Place this file at: m2c/tests/sandbox/test_netbridge.py
"""
from __future__ import annotations

import ast
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from m2c.sandbox import bridge


# --------------------------------------------------------------------------
# the module part
# --------------------------------------------------------------------------
def test_proxy_environment_sets_every_spelling_to_the_bridge_and_an_empty_no_proxy():
    env = bridge.proxy_environment()
    url = f"http://{bridge.BRIDGE_HOST}:{bridge.BRIDGE_PORT}"
    assert bridge.BRIDGE_HOST == "127.0.0.1"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[name] == url
    assert env["NO_PROXY"] == "" and env["no_proxy"] == ""
    assert set(env) == {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY",
                        "no_proxy"}


def test_apply_replaces_whatever_the_host_had_in_any_letter_case():
    host = {
        "PATH": "/usr/bin", "HOME": "/h",
        "http_proxy": "http://corp:3128", "HTTPS_PROXY": "http://corp:3128", "All_Proxy": "socks5://x:1",
        "no_proxy": "internal,.corp,localhost", "NO_PROXY": "*", "No_Proxy": "everything",
        "FTP_PROXY": "http://other",                                    # not one of ours: left as it is
    }
    out = bridge.apply_proxy_environment(host)
    assert out["PATH"] == "/usr/bin" and out["HOME"] == "/h" and out["FTP_PROXY"] == "http://other"
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert out[name] == bridge.proxy_url()
    assert out["NO_PROXY"] == "" and out["no_proxy"] == ""
    assert "All_Proxy" not in out and "No_Proxy" not in out
    assert host["NO_PROXY"] == "*"                                       # the input is not changed


def test_apply_on_an_empty_environment_gives_just_ours():
    assert bridge.apply_proxy_environment({}) == bridge.proxy_environment()


def test_launcher_argv_runs_the_mounted_script_with_isolated_python_and_ends_with_the_command():
    argv = bridge.launcher_argv("/usr/bin/python3", "/bin/bash", "echo 'hi there'; exit 3")
    assert argv[:3] == ["/usr/bin/python3", "-I", f"{bridge.NETWORK_DIR_DEST}/{bridge.SCRIPT_NAME}"]
    assert argv[argv.index("--port") + 1] == str(bridge.BRIDGE_PORT)
    assert argv[argv.index("--sock") + 1] == f"{bridge.NETWORK_DIR_DEST}/{bridge.SOCKET_NAME}"
    assert argv[-4:] == ["--", "/bin/bash", "-c", "echo 'hi there'; exit 3"]    # the command is ONE argument, never re-split


def test_the_command_is_passed_untouched_even_with_odd_characters():
    for command in ("", "-x", "--", "a\nb", "echo $HOME `id` ; $(x)", "ünï"):
        assert bridge.launcher_argv("/usr/bin/python3", "/bin/sh", command)[-1] == command


def test_network_mount_is_a_plain_frozen_record():
    mount = bridge.NetworkMount(directory="/d", python="/usr/bin/python3")
    assert (mount.directory, mount.python) == ("/d", "/usr/bin/python3")
    with pytest.raises(Exception):
        mount.directory = "/other"                                       # type: ignore[misc]


def test_bridge_source_is_this_very_file_and_never_imports_m2c():
    text = bridge.bridge_source()
    assert text == Path(bridge.__file__).read_text(encoding="utf-8")
    tree = ast.parse(text)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    stdlib = set(sys.stdlib_module_names)
    assert imported <= stdlib | {"__future__"}, imported - stdlib          # the script runs on the system python


def test_bridge_source_parses_with_python_38_grammar():
    ast.parse(bridge.bridge_source(), feature_version=(3, 8))


# --------------------------------------------------------------------------
# the script part, run for real
# --------------------------------------------------------------------------
class UnixEcho:
    """A unix-socket server on threads: it upper-cases what it receives (standing in for the proxy)."""

    def __init__(self, path: str):
        self.path = path
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(64)
        self.sock.settimeout(0.2)
        self.stop = threading.Event()
        self.connections = 0
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _serve(conn):
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                conn.sendall(data.upper())
            try:
                conn.shutdown(socket.SHUT_WR)
            except OSError:
                pass
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop.set()
        self.sock.close()
        self.thread.join(2)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def echo(tmp_path):
    server = UnixEcho(str(tmp_path / "proxy.sock"))
    yield server
    server.close()


def bridge_script(tmp_path) -> str:
    path = tmp_path / "bridge.py"
    path.write_text(bridge.bridge_source(), encoding="utf-8")
    return str(path)


def run_bridge(tmp_path, port, sock, command, *, timeout=20, extra_args=None):
    args = [sys.executable, "-I", bridge_script(tmp_path)] + (extra_args if extra_args is not None else
            ["--port", str(port), "--sock", sock, "--", *command])
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            start_new_session=True)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        raise
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)                    # the relay server left in the background
        except OSError:
            pass
    return proc.returncode, out.decode(), err.decode()


CLIENT = """
import socket, sys, threading
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])), 5)
def send():
    s.sendall(sys.argv[2].encode())
    s.shutdown(socket.SHUT_WR)
if sys.argv[2].startswith("generate:"):                 # a payload too big for an argument is made here
    sys.argv[2] = "ab" * int(sys.argv[2].split(":")[1])
threading.Thread(target=send, daemon=True).start()     # send and receive at once, as a real client does
data = b""
while True:
    try:
        chunk = s.recv(65536)
    except OSError:
        break
    if not chunk:
        break
    data += chunk
print(data.decode())
"""


def test_the_command_reaches_the_unix_socket_through_the_bridge(tmp_path, echo):
    port = free_port()
    code, out, err = run_bridge(tmp_path, port, echo.path, [sys.executable, "-c", CLIENT, str(port), "hello bridge"])
    assert code == 0, err
    assert out.strip() == "HELLO BRIDGE"
    assert echo.connections == 1


def test_a_large_payload_goes_both_ways_intact(tmp_path, echo):
    port = free_port()
    code, out, err = run_bridge(tmp_path, port, echo.path,
                                [sys.executable, "-c", CLIENT, str(port), "generate:600000"], timeout=60)
    assert code == 0, err
    assert out.strip() == "AB" * 600_000


def test_many_clients_at_once_are_all_served(tmp_path, echo):
    port = free_port()
    many = """
import socket, sys, threading
port = int(sys.argv[1])
results = []
def one(i):
    s = socket.create_connection(("127.0.0.1", port), 5)
    s.sendall(("msg%d" % i).encode())
    s.shutdown(socket.SHUT_WR)
    data = b""
    while True:
        c = s.recv(100)
        if not c:
            break
        data += c
    results.append(data.decode() == ("MSG%d" % i))
ts = [threading.Thread(target=one, args=(i,)) for i in range(40)]
[t.start() for t in ts]
[t.join() for t in ts]
print(len(results), all(results))
"""
    code, out, err = run_bridge(tmp_path, port, echo.path, [sys.executable, "-c", many, str(port)], timeout=60)
    assert code == 0, err
    assert out.split() == ["40", "True"]


def test_the_commands_exit_status_is_what_the_caller_sees(tmp_path, echo):
    for status in (0, 1, 7, 42):
        code, _, _ = run_bridge(tmp_path, free_port(), echo.path, ["/bin/sh", "-c", f"exit {status}"])
        assert code == status


def test_a_signal_that_ends_the_command_ends_the_process_the_same_way(tmp_path, echo):
    code, _, _ = run_bridge(tmp_path, free_port(), echo.path, ["/bin/sh", "-c", "kill -TERM $$"])
    assert code == -signal.SIGTERM                                 # exec'd: no wrapper process in between


def test_stdout_and_stderr_belong_to_the_command_and_the_background_relay_does_not_hold_them(tmp_path, echo):
    started = time.monotonic()
    code, out, err = run_bridge(tmp_path, free_port(), echo.path, ["/bin/sh", "-c", "echo to-out; echo to-err >&2"])
    assert (code, out, err) == (0, "to-out\n", "to-err\n")
    assert time.monotonic() - started < 10                          # the pipes closed when the command did


def test_the_command_runs_even_if_the_bridge_cannot_bind(tmp_path, echo):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        port = taken.getsockname()[1]
        # SO_REUSEADDR does not allow a second listener on a port that is listening: the bind fails
        code, out, err = run_bridge(tmp_path, port, echo.path, ["/bin/sh", "-c", "echo ran"])
    assert code == 0 and out == "ran\n"


def test_the_command_runs_even_if_the_socket_path_is_wrong_and_simply_has_no_network(tmp_path):
    port = free_port()
    code, out, err = run_bridge(tmp_path, port, str(tmp_path / "missing.sock"),
                                [sys.executable, "-c", CLIENT.replace("print(data.decode())", "print(repr(data.decode()))"),
                                 str(port), "x"])
    assert code == 0 and out.strip() == "''"                         # connected to the bridge, got nothing back


def test_a_command_that_cannot_be_started_is_127_with_fixed_text(tmp_path, echo):
    code, out, err = run_bridge(tmp_path, free_port(), echo.path, ["/nonexistent/binary-xyz"])
    assert code == 127
    assert "/nonexistent" not in err and "cannot run the command" in err


@pytest.mark.parametrize("args", [
    [], ["--port", "1"], ["--port", "x", "--sock", "s", "--", "true"], ["--sock", "s", "--", "true"],
    ["--port", "1", "--sock", "s"], ["--port", "1", "--sock", "s", "--"], ["--bogus", "--", "true"],
    ["--port"], ["--port", "1", "--sock"],
])
def test_bad_arguments_exit_2_and_run_nothing(tmp_path, args):
    marker = tmp_path / "ran"
    code, out, err = run_bridge(tmp_path, 0, "", [], extra_args=args + ([] if "--" in args else []))
    assert code == 2 and "bad arguments" in err
    assert not marker.exists()


def test_parse_is_strict():
    assert bridge._parse(["--port", "3128", "--sock", "/s", "--", "sh", "-c", "x"]) == (3128, "/s", ["sh", "-c", "x"])
    assert bridge._parse(["--sock", "/s", "--port", "5", "--", "a"]) == (5, "/s", ["a"])
    assert bridge._parse(["--port", "5", "--sock", "/s", "--", "a", "--port", "6"]) == (5, "/s", ["a", "--port", "6"])
    for bad in ([], ["--"], ["--port", "5", "--", "a"], ["--port", "five", "--sock", "/s", "--", "a"],
                ["x", "--port", "5", "--sock", "/s", "--", "a"]):
        assert bridge._parse(bad) is None


def test_the_bridge_listens_only_on_loopback(tmp_path, echo):
    port = free_port()
    probe = """
import socket, sys
for line in open("/proc/net/tcp").read().splitlines()[1:]:
    parts = line.split()
    if parts[3] == "0A" and int(parts[1].split(":")[1], 16) == int(sys.argv[1]):
        print(parts[1].split(":")[0])
"""
    code, out, err = run_bridge(tmp_path, port, echo.path, [sys.executable, "-c", probe, str(port)])
    assert code == 0, err
    assert out.split() == ["0100007F"]                              # 127.0.0.1 only, never 0.0.0.0
