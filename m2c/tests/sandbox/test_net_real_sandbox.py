"""
m2c/tests/sandbox/test_net_real_sandbox.py -- Patch C11b: the controlled network against the REAL bubblewrap.

Offline-safe: no real internet is touched.  The "internet" is a loopback server on the host that sends a banner and counts
its connections, reached only through the proxy's fake resolver and the proxy's test-only loopback flag (a constructor
argument, reachable from no setting or environment variable).  Every test checks what the command SAW and what the HOST
saw (the upstream's accept count, the policy's records), because a command can lie or fail quietly.

Proved here, for real:
* a command reaches an allowed local upstream through env proxy -> bridge -> unix socket -> proxy, and gets its bytes;
* a denied or unlisted host is never connected to; an IP literal, a private, metadata or loopback destination, a name
  that resolves to one, and a DNS rebinding are all refused even for an allowlisted name;
* the sandbox still has no network of its own: a direct connection (to an address, or to the host's loopback) fails;
* the proxy directory is read-only inside; talking to the unix socket directly gains nothing over the bridge;
* ``network="off"`` is the C11a sandbox: no mount, no proxy variables, nothing listening;
* through the shell tool: the environment (host proxy settings replaced, empty NO_PROXY), the prompt for a new domain,
  exit status and output of the command unchanged.

Needs bubblewrap, user namespaces and a system python3.  Otherwise every test is SKIPPED with a loud
"SKIPPED: <reason>" line on stderr and the reason in pytest's -rs summary; nothing here silently passes.

Run (Ubuntu / WSL2 Ubuntu):
    sudo apt install bubblewrap python3 && cd m2c && pip install -e ".[dev]" && pytest tests/sandbox/test_net_real_sandbox.py -v -rs

Place this file at: m2c/tests/sandbox/test_net_real_sandbox.py
"""
from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from m2c import sandbox
from m2c.sandbox import bridge, bwrap, netproxy
from m2c.sandbox.netpolicy import NetworkPolicy
from m2c.sandbox.netproxy import NetworkService, ProxyConfig
from m2c.tools import ToolContext
from m2c.tools.shell import AUTH_USER_APPROVED, ShellConfig, configure_shell, run_command

pytestmark = [
    pytest.mark.real_sandbox,
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the sandbox is Linux only"),
]

_STATUS: dict = {}


def _loud_skip(reason: str) -> None:
    sys.__stderr__.write(f"\nSKIPPED: real-sandbox network tests: {reason}\n")
    sys.__stderr__.flush()
    pytest.skip(reason)


@pytest.fixture(autouse=True)
def need_sandbox():
    if "result" not in _STATUS:
        _STATUS["result"] = sandbox.fresh_probe()
        _STATUS["python"] = bwrap.find_sandbox_python("/")
    result = _STATUS["result"]
    if not result.works:
        _loud_skip(result.describe())
    if not _STATUS["python"]:
        _loud_skip("no system python3 for the network bridge")


# --------------------------------------------------------------------------
# the fake internet
# --------------------------------------------------------------------------
class Upstream:
    """A loopback server on the host: sends a banner to whoever connects and counts connections."""

    def __init__(self):
        self.banner = "UPSTREAM-" + secrets.token_hex(6)
        self.accepted = 0
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            try:
                conn.sendall(self.banner.encode())
            except OSError:
                pass
            conn.close()

    def close(self):
        self._stop.set()
        self._sock.close()
        self._thread.join(2)


CLIENT = r'''
import os, socket, sys
mode, host, port = sys.argv[1], sys.argv[2], int(sys.argv[3])

def talk(s):
    s.settimeout(8)
    s.sendall(("CONNECT %s:%d HTTP/1.1\r\nHost: %s:%d\r\n\r\n" % (host, port, host, port)).encode())
    data = b""
    try:
        while len(data) < 4096:
            chunk = s.recv(1024)
            if not chunk:
                break
            data += chunk
            if data.startswith(b"HTTP/1.1 200") and data.count(b"\r\n\r\n") and len(data.split(b"\r\n\r\n", 1)[1]) >= 8:
                break
    except OSError:
        pass
    text = data.decode("latin-1")
    status = text.split(" ")[1] if text.startswith("HTTP/") else "0"
    print("STATUS", status)
    print("TEXT", text.replace("\r", "").replace("\n", "|"))

if mode == "via":
    proxy = os.environ["HTTPS_PROXY"]
    h, p = proxy.split("//")[1].split(":")
    talk(socket.create_connection((h, int(p)), 5))
elif mode == "unix":
    s = socket.socket(socket.AF_UNIX)
    s.connect("/run/m2c-net/proxy.sock")
    talk(s)
elif mode == "direct":
    try:
        socket.create_connection((host, port), 3).close()
        print("DIRECT CONNECTED")
    except OSError:
        print("DIRECT FAILED")
elif mode == "env":
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"):
        print("ENV", name, repr(os.environ.get(name)))
print("CLIENT_DONE")
'''


class Fixture:
    """A project with the client script, a NetworkService with a fake resolver, and a way to run commands in the sandbox."""

    def __init__(self, tmp_path: Path, allow=("allowed.example", "rebind.example"), hosts=None, prompt=None):
        self.home = Path(os.environ["HOME"]).resolve()
        self.project = self.home / "proj"
        self.project.mkdir(parents=True)
        (self.project / "netclient.py").write_text(CLIENT)
        self.upstream = Upstream()
        self.hosts = hosts if hosts is not None else {"allowed.example": ["127.0.0.1"]}
        self.resolved: list[str] = []
        self.records: list[dict] = []

        async def resolver(host, port):
            self.resolved.append(host)
            answer = self.hosts.get(host, [])
            return list(answer(host) if callable(answer) else answer)

        self.policy = NetworkPolicy(allow=allow, ports=(self.upstream.port,), prompt=prompt, record=self.records.append)
        self.service = NetworkService(self.policy, config=ProxyConfig(), resolver=resolver, _test_allow_loopback=True)
        self.service.start()
        self.python = _STATUS["python"]

    def run(self, command: str, *, network=True, timeout=40):
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "HOME": str(self.home)}
        kwargs = {}
        if network:
            env = bridge.apply_proxy_environment(env)
            kwargs["network"] = self.service.mount(self.python)
        argv = sandbox.wrap_command(command, project=str(self.project), env=env,
                                    toolchain_dirs=(), extra_ro=(), denied=sandbox.default_denied(), **kwargs)
        started = time.monotonic()
        proc = subprocess.run(argv, env=bwrap.outer_environment(), capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        proc.elapsed = time.monotonic() - started
        return proc

    def client(self, mode: str, host: str, port: int | None = None, **kw):
        port = self.upstream.port if port is None else port
        return self.run(f"{self.python} -I netclient.py {mode} {host} {port}", **kw)

    def close(self):
        self.service.close()
        self.upstream.close()


@pytest.fixture
def fx(tmp_path):
    f = Fixture(tmp_path)
    yield f
    f.close()


def parse(out: str):
    status = next((l.split()[1] for l in out.splitlines() if l.startswith("STATUS ")), None)
    text = next((l[5:] for l in out.splitlines() if l.startswith("TEXT ")), "")
    assert "CLIENT_DONE" in out, f"the client did not run to its end: {out!r}"
    return status, text


# --------------------------------------------------------------------------
# an allowed domain works from inside the sandbox
# --------------------------------------------------------------------------
def test_an_allowed_domain_works_from_inside_the_sandbox(fx):
    proc = fx.client("via", "allowed.example")
    status, text = parse(proc.stdout)
    assert status == "200" and fx.upstream.banner in text, proc.stderr
    assert fx.upstream.accepted == 1
    assert fx.resolved == ["allowed.example"]                       # resolved on the HOST, once
    assert {"host": "allowed.example", "decision": "allow", "scope": "listed"} in fx.records


def test_the_command_got_the_proxy_environment_and_nothing_else_about_the_network(fx):
    proc = fx.client("env", "x", 1)
    assert "CLIENT_DONE" in proc.stdout
    url = repr(bridge.proxy_url())
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert f"ENV {name} {url}" in proc.stdout
    assert "ENV NO_PROXY ''" in proc.stdout and "ENV no_proxy ''" in proc.stdout


def test_the_exit_status_and_output_of_the_command_are_unchanged(fx):
    for status in (0, 3, 42):
        assert fx.run(f"echo out; echo err >&2; exit {status}").returncode == status
    proc = fx.run("echo out; echo err >&2")
    assert proc.stdout == "out\n" and proc.stderr == "err\n"


def test_a_signal_still_ends_the_command_the_same_way(fx):
    proc = fx.run("kill -TERM $$")
    assert proc.returncode in (143, -15)


def test_the_background_bridge_does_not_keep_the_command_from_finishing(fx):
    proc = fx.run("true")
    assert proc.returncode == 0 and proc.elapsed < 15


def test_a_command_can_start_its_own_background_job_with_network_and_it_still_ends(fx):
    proc = fx.run(f"({fx.python} -I netclient.py via allowed.example {fx.upstream.port} > /dev/null &) ; sleep 0.5; echo done")
    assert proc.returncode == 0 and "done" in proc.stdout and proc.elapsed < 20


# --------------------------------------------------------------------------
# no unapproved egress
# --------------------------------------------------------------------------
def test_an_unlisted_host_is_denied_and_never_connected_to(fx):
    proc = fx.client("via", "unlisted.example")
    status, text = parse(proc.stdout)
    assert status == "403" and "not allowed" in text
    assert fx.upstream.accepted == 0 and fx.resolved == []
    assert fx.policy.unprompted_denials == 1


def test_a_prompt_that_denies_means_the_same(tmp_path):
    asked = []

    async def prompt(host, port, label):
        asked.append(host)
        return "deny"

    f = Fixture(tmp_path, prompt=prompt)
    try:
        proc = f.client("via", "new.example")
        status, _ = parse(proc.stdout)
        assert status == "403" and asked == ["new.example"] and f.upstream.accepted == 0 and f.resolved == []
    finally:
        f.close()


def test_a_prompt_that_allows_for_the_session_lets_the_command_through(tmp_path):
    async def prompt(host, port, label):
        return "allow_session"

    f = Fixture(tmp_path, prompt=prompt, hosts={"new.example": ["127.0.0.1"]})
    try:
        status, text = parse(f.client("via", "new.example").stdout)
        assert status == "200" and f.upstream.banner in text and f.upstream.accepted == 1
    finally:
        f.close()


@pytest.mark.parametrize("target", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "93.184.216.34", "[::1]", "2130706433"])
def test_ip_literals_are_refused(fx, target):
    status, text = parse(fx.client("via", target).stdout)
    assert status in ("400", "403")
    assert fx.upstream.accepted == 0 and fx.resolved == []


@pytest.mark.parametrize("answer", [["10.0.0.5"], ["169.254.169.254"], ["100.64.0.1"], ["::ffff:10.0.0.1"],
                                    ["fc00::1"], ["fe80::1"], ["0.0.0.0"], ["224.0.0.1"], ["127.0.0.1", "10.0.0.5"]])
def test_an_allowlisted_name_that_resolves_to_something_unsafe_is_refused(tmp_path, answer):
    f = Fixture(tmp_path, hosts={"allowed.example": answer})
    try:
        # (loopback itself is allowed by the probe-only flag in this fixture, so it is not in the list; a mixed answer such
        # as 127.0.0.1 + 10.0.0.5 is refused as a whole)
        status, text = parse(f.client("via", "allowed.example").stdout)
        assert status == "403" and "not reachable" in text
        assert f.upstream.accepted == 0
    finally:
        f.close()


def test_dns_rebinding_cannot_swap_the_destination_after_the_check(tmp_path):
    calls = []

    def answer(host):
        calls.append(host)
        return ["127.0.0.1"] if len(calls) == 1 else ["169.254.169.254"]

    f = Fixture(tmp_path, hosts={"allowed.example": answer})
    try:
        first, _ = parse(f.client("via", "allowed.example").stdout)
        second, text = parse(f.client("via", "allowed.example").stdout)
        assert first == "200" and second == "403" and "not reachable" in text
        assert f.upstream.accepted == 1                              # the second lookup was never connected to
        assert len(calls) == 2                                       # one lookup per connection, never two for one
    finally:
        f.close()


def test_other_ports_are_refused(fx):
    status, _ = parse(fx.client("via", "allowed.example", fx.upstream.port + 1 if fx.upstream.port < 65000 else 80).stdout)
    assert status == "403" and fx.upstream.accepted == 0


def test_plain_http_is_refused_with_the_fixed_text(fx):
    script = ("import os,socket;h,p=os.environ['HTTP_PROXY'].split('//')[1].split(':');"
              "s=socket.create_connection((h,int(p)),5);"
              "s.sendall(b'GET http://allowed.example/ HTTP/1.1\\r\\nHost: allowed.example\\r\\n\\r\\n');"
              "print(s.recv(2048).decode().replace(chr(13),'').replace(chr(10),'|'))")
    proc = fx.run(f"{fx.python} -I -c \"{script}\"")
    assert "403" in proc.stdout and "cleartext" in proc.stdout.lower() or "plain" in proc.stdout.lower()
    assert fx.upstream.accepted == 0


# --------------------------------------------------------------------------
# the sandbox still has no network of its own
# --------------------------------------------------------------------------
def test_a_direct_connection_fails_even_with_the_bridge_running(fx):
    assert "DIRECT FAILED" in fx.client("direct", "192.0.2.1", 443).stdout
    assert "DIRECT FAILED" in fx.client("direct", "93.184.216.34", 443).stdout
    assert "DIRECT FAILED" in fx.client("direct", "127.0.0.1").stdout       # the HOST's loopback is not the sandbox's
    assert fx.upstream.accepted == 0


def test_the_command_has_no_network_interface_but_loopback(fx):
    proc = fx.run("cat /proc/net/dev")
    names = [l.split(":")[0].strip() for l in proc.stdout.splitlines() if ":" in l]
    assert names == ["lo"]


def test_a_program_that_ignores_the_proxy_settings_simply_cannot_connect(fx):
    proc = fx.run(f"env -u HTTPS_PROXY -u https_proxy -u HTTP_PROXY -u http_proxy -u ALL_PROXY -u all_proxy "
                  f"{fx.python} -I netclient.py direct allowed.example 443")
    assert "DIRECT FAILED" in proc.stdout


# --------------------------------------------------------------------------
# the proxy's directory and socket
# --------------------------------------------------------------------------
def test_the_directory_is_read_only_inside_the_sandbox(fx):
    proc = fx.run("touch /run/m2c-net/x 2>/dev/null && echo WROTE; rm -f /run/m2c-net/bridge.py 2>/dev/null && echo REMOVED; "
                  "echo '#' >> /run/m2c-net/bridge.py 2>/dev/null && echo CHANGED; echo END")
    assert "END" in proc.stdout and not any(w in proc.stdout for w in ("WROTE", "REMOVED", "CHANGED"))
    assert sorted(os.listdir(fx.service.directory)) == sorted([bridge.SCRIPT_NAME, bridge.SOCKET_NAME])
    assert (Path(fx.service.directory) / bridge.SCRIPT_NAME).read_text() == bridge.bridge_source()


def test_talking_to_the_unix_socket_directly_gains_nothing_over_the_bridge(fx):
    denied, _ = parse(fx.client("unix", "unlisted.example").stdout)
    assert denied == "403" and fx.upstream.accepted == 0
    private_fx_hosts = fx.hosts
    private_fx_hosts["rebind.example"] = ["10.0.0.5"]
    refused, text = parse(fx.client("unix", "rebind.example").stdout)
    assert refused == "403" and "not reachable" in text and fx.upstream.accepted == 0
    ok, text = parse(fx.client("unix", "allowed.example").stdout)
    assert ok == "200"                                               # the same policy, the same answer as through the bridge


def test_the_proxy_directory_is_private_on_the_host(fx):
    st = os.stat(fx.service.directory)
    assert st.st_uid == os.geteuid() and (st.st_mode & 0o077) == 0
    assert (os.stat(fx.service.socket_path).st_mode & 0o077) == 0


def test_the_directory_is_not_visible_without_the_network(fx):
    proc = fx.run("ls /run/m2c-net 2>&1; echo END", network=False)
    assert "bridge.py" not in proc.stdout and "proxy.sock" not in proc.stdout


# --------------------------------------------------------------------------
# network off is the C11a sandbox
# --------------------------------------------------------------------------
def test_without_network_nothing_listens_and_no_proxy_variables_are_set(fx):
    proc = fx.client("via", "allowed.example", network=False)
    assert "CLIENT_DONE" not in proc.stdout                              # no HTTPS_PROXY, so the client stops at once
    proc = fx.run(f"{fx.python} -I netclient.py direct 127.0.0.1 {bridge.BRIDGE_PORT}", network=False)
    assert "DIRECT FAILED" in proc.stdout                                # nothing listens on the bridge port
    assert fx.upstream.accepted == 0


# --------------------------------------------------------------------------
# through the shell tool
# --------------------------------------------------------------------------
def shell_context(project: Path, **cfg) -> ToolContext:
    context = ToolContext(project)
    configure_shell(context, ShellConfig(sandbox="required", kill_grace_s=0.5, **cfg))
    return context


def sh(context, command, timeout_s=40):
    return run_command(context, command, timeout_s, authorization=AUTH_USER_APPROVED)


@pytest.fixture
def project(tmp_path):
    home = Path(os.environ["HOME"]).resolve()
    p = home / "proj"
    p.mkdir(parents=True)
    (p / "netclient.py").write_text(CLIENT)
    return p


def test_the_shell_tool_gives_a_sandboxed_command_the_proxy_and_replaces_the_hosts_settings(project, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://corp.example:3128")
    monkeypatch.setenv("no_proxy", "*")
    context = shell_context(project)
    try:
        out = sh(context, f"{_STATUS['python']} -I netclient.py env x 1")
        assert out.ok, out.output
        assert f"ENV HTTP_PROXY {bridge.proxy_url()!r}" in out.output and "corp.example" not in out.output
        assert "ENV no_proxy ''" in out.output and "ENV NO_PROXY ''" in out.output
    finally:
        netproxy.close_network(context)


def test_the_shell_tool_denies_an_unlisted_domain_without_a_prompt_and_asks_with_one(project):
    asked = []

    async def prompt(host, port, label):
        asked.append((host, port, label))
        return "deny"

    context = shell_context(project, network_prompt=prompt)
    try:
        out = sh(context, f"{_STATUS['python']} -I netclient.py via unlisted.example 443")
        assert "STATUS 403" in out.output, out.output
        assert asked and asked[0][:2] == ("unlisted.example", 443)
        assert "netclient.py" in asked[0][2] or asked[0][2].startswith(_STATUS["python"])      # the short label only
    finally:
        netproxy.close_network(context)


def test_the_shell_tool_without_a_prompt_is_a_headless_deny(project):
    context = shell_context(project)
    try:
        out = sh(context, f"{_STATUS['python']} -I netclient.py via unlisted.example 443")
        assert "STATUS 403" in out.output
        assert netproxy._SERVICES[context].unprompted_denials == 1
    finally:
        netproxy.close_network(context)


def test_the_shell_tool_with_network_off_is_the_c11a_sandbox(project):
    context = shell_context(project, network="off")
    out = sh(context, f"{_STATUS['python']} -I netclient.py env x 1; ls /run/m2c-net 2>&1 | head -1")
    assert "CLIENT_DONE" in out.output
    assert bridge.proxy_url() not in out.output                          # none of OUR variables (the host's own are C11a's)
    assert "No such file or directory" in out.output                     # nothing is mounted at /run/m2c-net
    assert netproxy._SERVICES.get(context) is None


def test_the_shell_tools_default_is_a_working_sandbox_with_a_proxy_and_the_pre_allowed_list_reaches_it(project):
    context = shell_context(project, network_allow=("pre.example",))
    try:
        out = sh(context, "echo hello")
        assert out.ok and "hello" in out.output
        assert netproxy._SERVICES[context].policy.check("pre.example", 443).action == "allow"
    finally:
        netproxy.close_network(context)


def test_the_proxy_dies_with_the_session_and_leaves_no_directory(project):
    context = shell_context(project)
    sh(context, "true")
    directory = netproxy._SERVICES[context].directory
    assert os.path.isdir(directory)
    netproxy.close_network(context)
    assert not os.path.exists(directory)
