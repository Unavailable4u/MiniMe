"""
m2c/tests/sandbox/test_netproxy.py -- Patch C11b: the host-side proxy, against a fake resolver and a fake upstream.

Nothing here touches the internet: the resolver is a dict, the "upstream" is an echo server on loopback that the fake
connector reaches no matter what address the proxy pinned (the pinned address is RECORDED, which is the evidence).

What is proven: a denied or unlisted host is never resolved or connected to; private, loopback, link-local/metadata,
CGNAT, unique-local, multicast, reserved and IPv4-mapped destinations are refused even for allowlisted names; DNS is
resolved ONCE and the connection goes to that address (rebinding); one bad address in an answer refuses the whole
answer; plain http and non-CONNECT requests are refused with fixed text; oversized request lines and headers, slow
heads and idle tunnels are cut; connection caps hold; shutdown closes everything and removes the directory; the
test-only loopback flag is reachable from no setting, environment variable or production constructor.

Place this file at: m2c/tests/sandbox/test_netproxy.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import secrets
import stat
from pathlib import Path

import pytest

from m2c.sandbox import bridge, netproxy
from m2c.sandbox.netpolicy import NetworkPolicy
from m2c.sandbox.netproxy import NetworkError, ProxyConfig, ProxyServer, RESPONSES, blocked_address

PUBLIC = "93.184.216.34"


def reply_text(key: str) -> bytes:
    status, reason, body = RESPONSES[key]
    return f"HTTP/1.1 {status} {reason}".encode()


class Rig:
    """A running ProxyServer plus the fakes around it."""

    def __init__(self, *, allow=("allowed.example",), ports=(443,), hosts=None, config=None, prompt=None,
                 loopback=False, record=None, resolver=None, connector=None):
        self.hosts = {"allowed.example": [PUBLIC]} if hosts is None else hosts
        self.resolved: list[tuple[str, int]] = []
        self.connects: list[tuple[str, int]] = []
        self.records: list[dict] = [] if record is None else record
        self.policy = NetworkPolicy(allow=allow, ports=ports, prompt=prompt, record=self.records.append,
                                    deny_cooldown_s=0)
        self._resolver = resolver
        self._connector = connector
        self.config = config or ProxyConfig()
        self.loopback = loopback
        self.server: ProxyServer | None = None
        self._upstream = None
        self.up_port = 0
        self.upstream_clients: list[asyncio.StreamWriter] = []

    async def _resolve(self, host, port):
        self.resolved.append((host, port))
        answer = self.hosts.get(host, [])
        return answer(host, port) if callable(answer) else list(answer)

    async def _connect(self, ip, port):
        self.connects.append((ip, port))
        return await asyncio.open_connection("127.0.0.1", self.up_port)

    async def _echo(self, reader, writer):
        self.upstream_clients.append(writer)
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                writer.write(data.upper())
                await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            writer.close()

    async def __aenter__(self):
        self._upstream = await asyncio.start_server(self._echo, "127.0.0.1", 0)
        self.up_port = self._upstream.sockets[0].getsockname()[1]
        self.server = ProxyServer(
            self.policy, config=self.config, resolver=self._resolver or self._resolve,
            connector=self._connector or self._connect, _test_allow_loopback=self.loopback)
        await self.server.start()
        return self

    async def __aexit__(self, *exc):
        await self.server.aclose()
        self._upstream.close()
        await self._upstream.wait_closed()

    async def open(self):
        return await asyncio.open_unix_connection(self.server.socket_path)

    async def ask(self, raw: bytes, *, read: int = 4096, timeout: float = 5.0) -> bytes:
        """Send ``raw`` and read what comes back until the proxy closes (or ``timeout``)."""
        reader, writer = await self.open()
        try:
            writer.write(raw)
            await writer.drain()
            out = b""
            try:
                async with asyncio.timeout(timeout):
                    while True:
                        chunk = await reader.read(read)
                        if not chunk:
                            break
                        out += chunk
            except (TimeoutError, ConnectionError):      # the proxy hung up on unread input: what came before is kept
                pass
            return out
        finally:
            writer.close()

    async def connect(self, target: str, extra: bytes = b""):
        """CONNECT ``target``; returns (reader, writer, first line) with the tunnel left open on success."""
        reader, writer = await self.open()
        writer.write(f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode() + extra)
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        return reader, writer, head


def run(coro):
    return asyncio.run(coro)


def fixed(key):
    status, reason, body = RESPONSES[key]
    return status, body


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------
def test_an_allowed_host_is_tunnelled_through_the_pinned_address():
    async def go():
        async with Rig() as rig:
            reader, writer, head = await rig.connect("allowed.example:443")
            writer.write(b"ping")
            await writer.drain()
            echoed = await asyncio.wait_for(reader.readexactly(4), 5)
            writer.close()
            return head, echoed, rig.resolved, rig.connects

    head, echoed, resolved, connects = run(go())
    assert head.startswith(b"HTTP/1.1 200")
    assert echoed == b"PING"
    assert resolved == [("allowed.example", 443)]
    assert connects == [(PUBLIC, 443)]


def test_bytes_sent_right_after_the_connect_request_are_not_lost():
    async def go():
        async with Rig() as rig:
            reader, writer, head = await rig.connect("allowed.example:443", extra=b"early")
            echoed = await asyncio.wait_for(reader.readexactly(5), 5)
            writer.close()
            return echoed

    assert run(go()) == b"EARLY"


def test_a_half_close_from_the_client_still_lets_the_reply_through():
    async def go():
        async with Rig() as rig:
            reader, writer, _ = await rig.connect("allowed.example:443")
            writer.write(b"abc")
            await writer.drain()
            writer.write_eof()
            data = await asyncio.wait_for(reader.read(), 5)
            writer.close()
            return data

    assert run(go()) == b"ABC"


def test_the_host_in_the_request_target_decides_not_the_host_header():
    async def go():
        async with Rig() as rig:
            raw = b"CONNECT unlisted.example:443 HTTP/1.1\r\nHost: allowed.example:443\r\n\r\n"
            return await rig.ask(raw), rig

    out, rig = run(go())
    assert out.startswith(reply_text("denied"))
    assert rig.connects == [] and rig.resolved == []


# --------------------------------------------------------------------------
# no unapproved egress
# --------------------------------------------------------------------------
def test_an_unlisted_host_without_a_prompt_is_denied_and_never_resolved_or_connected():
    async def go():
        async with Rig() as rig:
            out = await rig.ask(b"CONNECT unlisted.example:443 HTTP/1.1\r\n\r\n")
            return out, rig.resolved, rig.connects, rig.policy.unprompted_denials, rig.server.stats

    out, resolved, connects, unprompted, stats = run(go())
    status, body = fixed("denied")
    assert out.startswith(f"HTTP/1.1 {status} ".encode()) and body.encode() in out
    assert resolved == [] and connects == []
    assert unprompted == 1
    assert stats["tunnels"] == 0 and stats["refused"] == 1


def test_a_prompt_that_says_deny_means_nothing_is_ever_connected():
    async def go():
        async def prompt(host, port, label):
            return "deny"

        async with Rig(prompt=prompt) as rig:
            out = await rig.ask(b"CONNECT unlisted.example:443 HTTP/1.1\r\n\r\n")
            return out, rig.resolved, rig.connects

    out, resolved, connects = run(go())
    assert out.startswith(reply_text("denied"))
    assert resolved == [] and connects == []


def test_a_prompt_that_allows_for_the_session_lets_the_next_request_through_without_asking():
    async def go():
        asked = []

        async def prompt(host, port, label):
            asked.append((host, port))
            return "allow_session"

        async with Rig(allow=(), hosts={"new.example": [PUBLIC]}, prompt=prompt) as rig:
            for _ in range(2):
                reader, writer, head = await rig.connect("new.example:443")
                assert head.startswith(b"HTTP/1.1 200")
                writer.close()
            return asked, rig.connects

    asked, connects = run(go())
    assert asked == [("new.example", 443)]
    assert connects == [(PUBLIC, 443)] * 2


def test_many_connections_to_one_unlisted_host_raise_one_prompt():
    async def go():
        gate = asyncio.Event()
        asked = []

        async def prompt(host, port, label):
            asked.append(host)
            await gate.wait()
            return "allow_session"

        async with Rig(allow=(), hosts={"new.example": [PUBLIC]}, prompt=prompt) as rig:
            tasks = [asyncio.ensure_future(rig.connect("new.example:443")) for _ in range(10)]
            await asyncio.sleep(0.2)
            assert asked == ["new.example"]
            gate.set()
            results = await asyncio.gather(*tasks)
            for _, writer, head in results:
                assert head.startswith(b"HTTP/1.1 200")
                writer.close()
            return asked

    assert run(go()) == ["new.example"]


def test_the_prompt_gets_only_the_host_the_port_and_the_short_label():
    async def go():
        seen = []

        async def prompt(host, port, label):
            seen.append((host, port, label))
            return "deny"

        async with Rig(allow=(), prompt=prompt) as rig:
            rig.server._label = lambda: "pip install thing"
            await rig.ask(b"CONNECT unlisted.example:443 HTTP/1.1\r\nX-Secret: header-value\r\n\r\n")
            return seen

    assert run(go()) == [("unlisted.example", 443, "pip install thing")]


@pytest.mark.parametrize("ip", [
    "10.0.0.5", "172.16.0.1", "172.31.255.255", "192.168.1.1",                       # RFC 1918
    "127.0.0.1", "127.255.255.255", "::1",                                          # loopback
    "169.254.169.254", "169.254.1.1", "fe80::1", "168.63.129.16", "fd00:ec2::254",   # link-local and cloud metadata
    "100.64.0.1", "100.127.255.255",                                                # CGNAT
    "fc00::1", "fd12:3456::1",                                                      # unique-local
    "224.0.0.1", "ff02::1",                                                         # multicast
    "0.0.0.0", "::", "240.0.0.1", "255.255.255.255", "192.0.0.1", "198.18.0.1",     # unspecified and reserved
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254",                # IPv4-mapped IPv6
    "64:ff9b::7f00:1", "2002:7f00:1::", "2001:db8::1",                              # NAT64 / 6to4 embedding, documentation
])
def test_unsafe_destinations_are_refused_even_for_an_allowlisted_name(ip):
    async def go():
        async with Rig(hosts={"allowed.example": [ip]}) as rig:
            out = await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
            return out, rig.connects, rig.records

    out, connects, records = run(go())
    status, body = fixed("destination")
    assert out.startswith(f"HTTP/1.1 {status} ".encode()) and body.encode() in out
    assert connects == []                                                           # never connected
    assert {"host": "allowed.example", "decision": "deny", "scope": "address"} in records


@pytest.mark.parametrize("answer", [
    [PUBLIC, "10.0.0.5"], ["10.0.0.5", PUBLIC], [PUBLIC, "169.254.169.254"], [PUBLIC, "::1"],
    [PUBLIC, "100.64.0.1", "8.8.8.8"], ["::ffff:192.168.0.1", PUBLIC],
])
def test_one_unsafe_address_in_the_answer_refuses_the_whole_answer(answer):
    async def go():
        async with Rig(hosts={"allowed.example": answer}) as rig:
            out = await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
            return out, rig.connects

    out, connects = run(go())
    assert out.startswith(reply_text("destination"))
    assert connects == []


def test_dns_is_resolved_once_and_the_connection_goes_to_that_answer_rebinding_cannot_swap_it():
    async def go():
        calls = []

        def answer(host, port):
            calls.append(host)
            return [PUBLIC] if len(calls) == 1 else ["169.254.169.254"]       # a rebinding server: public, then metadata

        async with Rig(hosts={"allowed.example": answer}) as rig:
            reader, writer, head = await rig.connect("allowed.example:443")
            writer.close()
            out2 = await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")   # the next lookup is the bad one
            return head, calls, rig.connects, out2

    head, calls, connects, out2 = run(go())
    assert head.startswith(b"HTTP/1.1 200")
    assert connects[0] == (PUBLIC, 443)                          # connected to the FIRST answer's address
    assert calls == ["allowed.example"] * 2                      # once per connection, never twice for one
    assert out2.startswith(reply_text("destination")) and len(connects) == 1


def test_the_connector_is_only_ever_given_an_ip_address_never_a_name():
    async def go():
        async with Rig(hosts={"allowed.example": [PUBLIC, "8.8.8.8"]}) as rig:
            reader, writer, _ = await rig.connect("allowed.example:443")
            writer.close()
            return rig.connects

    import ipaddress

    for ip, port in run(go()):
        ipaddress.ip_address(ip)


def test_the_real_connector_refuses_a_name():
    async def go():
        with pytest.raises((ValueError, OSError)):
            await netproxy.default_connector("example.com", 443)

    run(go())


def test_each_resolved_address_is_tried_in_turn():
    async def go():
        tried = []

        async def connector(ip, port):
            tried.append(ip)
            if ip != "8.8.4.4":
                raise ConnectionRefusedError()
            return await asyncio.open_connection("127.0.0.1", rig.up_port)

        async with Rig(hosts={"allowed.example": ["8.8.8.8", "1.1.1.1", "8.8.4.4"]}, connector=connector) as rig:
            reader, writer, head = await rig.connect("allowed.example:443")
            writer.close()
            return head, tried

    head, tried = run(go())
    assert head.startswith(b"HTTP/1.1 200")
    assert tried == ["8.8.8.8", "1.1.1.1", "8.8.4.4"]


def test_when_every_address_fails_the_answer_is_a_fixed_bad_gateway():
    async def go():
        async def connector(ip, port):
            raise ConnectionRefusedError()

        async with Rig(connector=connector) as rig:
            return await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")

    assert run(go()).startswith(reply_text("unreachable"))


def test_a_connect_that_hangs_is_cut_after_the_connect_timeout():
    async def go():
        async def connector(ip, port):
            await asyncio.sleep(30)

        async with Rig(connector=connector, config=ProxyConfig(connect_timeout_s=0.1)) as rig:
            return await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n", timeout=3)

    assert run(go()).startswith(reply_text("unreachable"))


def test_at_most_max_addresses_are_tried():
    async def go():
        async def connector(ip, port):
            tried.append(ip)
            raise ConnectionRefusedError()

        tried: list[str] = []
        many = [f"8.8.8.{i}" for i in range(1, 11)]
        async with Rig(hosts={"allowed.example": many}, connector=connector, config=ProxyConfig(max_addresses=3)) as rig:
            await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
        return tried

    assert len(run(go())) == 3


def test_a_resolver_that_fails_or_hangs_or_answers_nothing_is_a_refusal_not_a_connection():
    async def go():
        async def broken(host, port):
            raise OSError("no dns")

        async def hangs(host, port):
            await asyncio.sleep(30)

        out = []
        for resolver, cfg in ((broken, ProxyConfig()), (hangs, ProxyConfig(resolve_timeout_s=0.1))):
            async with Rig(resolver=resolver, config=cfg) as rig:
                out.append((await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n", timeout=3), rig.connects))
        async with Rig(hosts={"allowed.example": []}) as rig:
            out.append((await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n"), rig.connects))
        return out

    for reply, connects in run(go()):
        assert reply.startswith(reply_text("unreachable")) and connects == []


def test_an_answer_with_too_many_addresses_is_refused():
    async def go():
        many = [f"8.8.{i // 250}.{i % 250 + 1}" for i in range(40)]
        async with Rig(hosts={"allowed.example": many}) as rig:
            return await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n"), rig.connects

    out, connects = run(go())
    assert out.startswith(reply_text("destination")) and connects == []


def test_ip_literals_are_denied_unless_listed_and_a_listed_private_one_is_still_refused():
    async def go():
        async with Rig(allow=("allowed.example", "10.0.0.9", "93.184.216.35")) as rig:
            unlisted = await rig.ask(b"CONNECT 93.184.216.34:443 HTTP/1.1\r\n\r\n")
            private = await rig.ask(b"CONNECT 10.0.0.9:443 HTTP/1.1\r\n\r\n")
            metadata = await rig.ask(b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n")
            ok_reader, ok_writer, ok_head = await rig.connect("93.184.216.35:443")
            ok_writer.close()
            return unlisted, private, metadata, ok_head, rig.resolved, rig.connects

    unlisted, private, metadata, ok_head, resolved, connects = run(go())
    assert unlisted.startswith(reply_text("denied"))
    assert private.startswith(reply_text("destination"))                 # listed, but private: refused
    assert metadata.startswith(reply_text("denied"))                     # not listed
    assert ok_head.startswith(b"HTTP/1.1 200")
    assert resolved == []                                                # a literal is never sent to DNS
    assert connects == [("93.184.216.35", 443)]


def test_odd_spellings_of_loopback_are_denied_by_the_policy_before_anything_happens():
    async def go():
        async with Rig(allow=("allowed.example",)) as rig:
            outs = [await rig.ask(f"CONNECT {t}:443 HTTP/1.1\r\n\r\n".encode())
                    for t in ("2130706433", "0x7f.1", "127.1", "localhost", "[::1]", "[::ffff:127.0.0.1]", "a.localhost")]
            return outs, rig.resolved, rig.connects

    outs, resolved, connects = run(go())
    assert all(o.startswith(b"HTTP/1.1 400") or o.startswith(b"HTTP/1.1 403") for o in outs)
    assert resolved == [] and connects == []


@pytest.mark.parametrize("port", [80, 8443, 22, 1])
def test_other_ports_are_denied(port):
    async def go():
        async with Rig() as rig:
            return await rig.ask(f"CONNECT allowed.example:{port} HTTP/1.1\r\n\r\n".encode()), rig.connects

    out, connects = run(go())
    assert out.startswith(reply_text("denied")) and connects == []


def test_a_configured_port_works():
    async def go():
        async with Rig(ports=(8443,)) as rig:
            reader, writer, head = await rig.connect("allowed.example:8443")
            writer.close()
            return head, rig.connects

    head, connects = run(go())
    assert head.startswith(b"HTTP/1.1 200") and connects == [(PUBLIC, 8443)]


# --------------------------------------------------------------------------
# plain http, other methods, malformed requests
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", [
    b"GET http://allowed.example/ HTTP/1.1\r\nHost: allowed.example\r\n\r\n",
    b"POST http://allowed.example:80/x HTTP/1.1\r\n\r\n",
    b"GET http://unlisted.example/ HTTP/1.1\r\n\r\n",
    b"GET HTTP://ALLOWED.example/ HTTP/1.0\r\n\r\n",
])
def test_plain_http_gets_the_fixed_refusal_by_default(raw):
    async def go():
        async with Rig(ports=(80, 443)) as rig:
            return await rig.ask(raw), rig.resolved, rig.connects

    out, resolved, connects = run(go())
    status, body = fixed("cleartext")
    assert out.startswith(f"HTTP/1.1 {status} ".encode()) and body.encode() in out
    assert resolved == [] and connects == []


def test_plain_http_can_be_allowed_by_a_setting_and_is_still_filtered_by_the_policy():
    async def go():
        async with Rig(ports=(80,), config=ProxyConfig(allow_cleartext=True)) as rig:
            ok_reader, ok_writer = await rig.open()
            ok_writer.write(b"GET http://allowed.example/path HTTP/1.1\r\nHost: allowed.example\r\n\r\n")
            await ok_writer.drain()
            echoed = await asyncio.wait_for(ok_reader.readuntil(b"\r\n\r\n"), 5)
            ok_writer.close()
            denied = await rig.ask(b"GET http://unlisted.example/ HTTP/1.1\r\n\r\n")
            wrong_port = await rig.ask(b"GET http://allowed.example:8080/ HTTP/1.1\r\n\r\n")
            return echoed, denied, wrong_port, rig.connects

    echoed, denied, wrong_port, connects = run(go())
    assert echoed.startswith(b"GET HTTP://ALLOWED.EXAMPLE/PATH")        # the request was forwarded (the fake upper-cases it)
    assert denied.startswith(reply_text("denied")) and wrong_port.startswith(reply_text("denied"))
    assert connects == [(PUBLIC, 80)]


@pytest.mark.parametrize("raw", [
    b"GET / HTTP/1.1\r\n\r\n", b"POST /x HTTP/1.1\r\n\r\n", b"DELETE /x HTTP/1.1\r\n\r\n", b"OPTIONS * HTTP/1.1\r\n\r\n",
    b"PUT http2://allowed.example/ HTTP/1.1\r\n\r\n", b"connect allowed.example:443 HTTP/1.1\r\n\r\n",
    b"GET ftp://allowed.example/ HTTP/1.1\r\n\r\n", b"GET //allowed.example/ HTTP/1.1\r\n\r\n",
])
def test_anything_but_connect_or_an_absolute_http_url_is_method_not_allowed(raw):
    async def go():
        async with Rig() as rig:
            return await rig.ask(raw), rig.resolved, rig.connects

    out, resolved, connects = run(go())
    assert out.startswith(reply_text("method")) and resolved == [] and connects == []


@pytest.mark.parametrize("raw", [
    b"CONNECT allowed.example HTTP/1.1\r\n\r\n",                      # no port
    b"CONNECT allowed.example:0 HTTP/1.1\r\n\r\n",
    b"CONNECT allowed.example:99999 HTTP/1.1\r\n\r\n",
    b"CONNECT allowed.example:443 HTTP/2.0\r\n\r\n",
    b"CONNECT allowed.example:443\r\n\r\n",
    b"CONNECT  allowed.example:443 HTTP/1.1\r\n\r\n",                  # double space
    b"CONNECT allowed.example:443@evil.example:443 HTTP/1.1\r\n\r\n",   # userinfo trick
    b"CONNECT allowed.example:443/path HTTP/1.1\r\n\r\n",
    b"CONNECT allowed.example:443,evil.example:443 HTTP/1.1\r\n\r\n",
    b"CONNECT \xff\xfe:443 HTTP/1.1\r\n\r\n",
    b"CONNECT allowed.example:443 HTTP/1.1\x00\r\n\r\n",
    b"\x16\x03\x01\x02\x00\x01\x00\x01\xfc\x03\x03" + b"\r\n\r\n",       # a TLS hello sent to a proxy port
])
def test_malformed_requests_get_the_fixed_bad_request(raw):
    async def go():
        async with Rig() as rig:
            return await rig.ask(raw), rig.resolved, rig.connects

    out, resolved, connects = run(go())
    assert out.startswith(reply_text("bad")) or out.startswith(reply_text("method")) or out == b""
    assert resolved == [] and connects == []


def test_a_client_that_disconnects_before_finishing_the_request_is_just_dropped():
    async def go():
        async with Rig() as rig:
            reader, writer = await rig.open()
            writer.write(b"CONNECT allowed.exa")
            await writer.drain()
            writer.close()
            await asyncio.sleep(0.2)
            return rig.server._active, rig.resolved, rig.connects

    active, resolved, connects = run(go())
    assert active == 0 and resolved == [] and connects == []


def test_refusal_texts_are_fixed_and_hold_no_host_path_or_header():
    async def go():
        secret = "tok" + secrets.token_hex(8)
        async with Rig(ports=(80, 443)) as rig:
            outs = [
                await rig.ask(f"CONNECT unlisted-{secret}.example:443 HTTP/1.1\r\nAuthorization: {secret}\r\n\r\n".encode()),
                await rig.ask(f"GET http://{secret}.example/{secret}?k={secret} HTTP/1.1\r\n\r\n".encode()),
                await rig.ask(f"GET /{secret} HTTP/1.1\r\n\r\n".encode()),
            ]
        return secret, outs

    secret, outs = run(go())
    for out in outs:
        assert secret.encode() not in out
        assert out.count(b"HTTP/1.1") == 1


# --------------------------------------------------------------------------
# size limits and timeouts
# --------------------------------------------------------------------------
def test_an_oversized_request_line_is_cut():
    async def go():
        async with Rig() as rig:
            long_line = b"CONNECT " + b"a" * 5000 + b".example:443 HTTP/1.1\r\n\r\n"
            return await rig.ask(long_line), rig.resolved

    out, resolved = run(go())
    assert out.startswith(reply_text("too_large")) and resolved == []


def test_a_request_line_with_no_end_is_cut_as_soon_as_it_is_too_long():
    async def go():
        async with Rig() as rig:
            return await rig.ask(b"CONNECT " + b"a" * 4000)       # never sends a newline; the proxy must not wait for one

    assert run(go()).startswith(reply_text("too_large"))


def test_oversized_headers_are_cut_whether_one_big_one_or_many_small_ones():
    async def go():
        async with Rig() as rig:
            big = b"CONNECT allowed.example:443 HTTP/1.1\r\nX-Big: " + b"a" * 20000 + b"\r\n\r\n"
            many = b"CONNECT allowed.example:443 HTTP/1.1\r\n" + b"X-A: b\r\n" * 3000 + b"\r\n"
            endless = b"CONNECT allowed.example:443 HTTP/1.1\r\nX-A: " + b"b" * 30000    # no terminator at all
            return [await rig.ask(x) for x in (big, many, endless)], rig.resolved, rig.connects

    outs, resolved, connects = run(go())
    assert all(o.startswith(reply_text("too_large")) for o in outs)
    assert resolved == [] and connects == []


def test_headers_just_under_the_limit_are_fine():
    async def go():
        async with Rig() as rig:
            pad = b"X-Pad: " + b"a" * 1500 + b"\r\n"
            raw = b"CONNECT allowed.example:443 HTTP/1.1\r\n" + pad * 4 + b"\r\n"
            assert len(raw) < rig.config.max_header_bytes
            reader, writer = await rig.open()
            writer.write(raw)
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            writer.close()
            return head

    assert run(go()).startswith(b"HTTP/1.1 200")


def test_slowloris_a_head_that_never_finishes_is_cut_and_frees_its_slot():
    async def go():
        async with Rig(config=ProxyConfig(header_timeout_s=0.2)) as rig:
            reader, writer = await rig.open()
            writer.write(b"CONNECT allowed.example:443 HTTP/1.1\r\nX-Slow: a")      # then silence
            await writer.drain()
            data = await asyncio.wait_for(reader.read(), 5)
            await asyncio.sleep(0.05)
            writer.close()
            return data, rig.server._active, rig.resolved

    data, active, resolved = run(go())
    assert data.startswith(reply_text("timeout"))
    assert active == 0 and resolved == []


def test_slowloris_a_head_dribbled_one_byte_at_a_time_is_cut_by_the_overall_deadline():
    async def go():
        async with Rig(config=ProxyConfig(header_timeout_s=0.3)) as rig:
            reader, writer = await rig.open()
            cut = False
            for byte in b"CONNECT allowed.example:443 HTTP/1.1\r\nX-A: " + b"a" * 200:
                try:
                    writer.write(bytes([byte]))
                    await writer.drain()
                except (ConnectionError, OSError):
                    cut = True
                    break
                await asyncio.sleep(0.05)                                # slow, but each gap is under the timeout
            try:
                data = await asyncio.wait_for(reader.read(), 5)
            except (ConnectionError, OSError):                          # the proxy already hung up on us
                data = b""
            writer.close()
            return cut, data

    cut, data = run(go())
    assert data.startswith(reply_text("timeout")) or cut or data == b""


def test_an_idle_tunnel_is_closed_after_the_idle_timeout_but_a_busy_one_is_not():
    async def go():
        async with Rig(config=ProxyConfig(idle_timeout_s=0.4)) as rig:
            busy_reader, busy_writer, _ = await rig.connect("allowed.example:443")
            idle_reader, idle_writer, _ = await rig.connect("allowed.example:443")
            for _ in range(6):                                           # 0.9 s of steady traffic on one tunnel
                busy_writer.write(b"x")
                await busy_writer.drain()
                assert await asyncio.wait_for(busy_reader.readexactly(1), 2) == b"X"
                await asyncio.sleep(0.15)
            idle_closed = await asyncio.wait_for(idle_reader.read(), 3) == b""
            busy_writer.close()
            idle_writer.close()
            return idle_closed

    assert run(go()) is True


# --------------------------------------------------------------------------
# caps
# --------------------------------------------------------------------------
def test_the_overall_connection_cap_holds_and_frees_up():
    async def go():
        async with Rig(config=ProxyConfig(max_connections=3, header_timeout_s=5)) as rig:
            held = [await rig.open() for _ in range(3)]
            for _, writer in held:                                      # three half-sent requests occupy the slots
                writer.write(b"CONNECT allowed.example:443 HTTP/1.1\r\n")
                await writer.drain()
            await asyncio.sleep(0.1)
            refused = await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
            for _, writer in held:
                writer.close()
            await asyncio.sleep(0.2)
            reader, writer, head = await rig.connect("allowed.example:443")
            writer.close()
            return refused, head, rig.server.stats

    refused, head, stats = run(go())
    assert refused == b"" or refused.startswith(reply_text("busy"))     # a hang-up may overtake the text; never a tunnel
    assert head.startswith(b"HTTP/1.1 200")
    assert stats["busy"] >= 1


def test_the_per_host_cap_holds_while_other_hosts_still_work():
    async def go():
        hosts = {"allowed.example": [PUBLIC], "other.example": ["8.8.8.8"]}
        async with Rig(allow=("allowed.example", "other.example"), hosts=hosts,
                       config=ProxyConfig(max_per_host=2)) as rig:
            tunnels = [await rig.connect("allowed.example:443") for _ in range(2)]
            third = await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n")
            other_reader, other_writer, other_head = await rig.connect("other.example:443")
            other_writer.close()
            tunnels[0][1].close()
            await asyncio.sleep(0.2)
            again_reader, again_writer, again_head = await rig.connect("allowed.example:443")
            again_writer.close()
            tunnels[1][1].close()
            return third, other_head, again_head, rig.server._per_host

    third, other_head, again_head, per_host = run(go())
    assert third.startswith(reply_text("busy"))
    assert other_head.startswith(b"HTTP/1.1 200") and again_head.startswith(b"HTTP/1.1 200")


def test_counters_return_to_zero_after_a_burst_of_mixed_connections():
    async def go():
        async with Rig() as rig:
            jobs = []
            for i in range(30):
                target = b"allowed.example:443" if i % 3 else b"unlisted.example:443"
                jobs.append(rig.ask(b"CONNECT " + target + b" HTTP/1.1\r\n\r\n", timeout=1))
            await asyncio.gather(*jobs)
            await asyncio.sleep(0.3)
            return rig.server._active, rig.server._per_host, len(rig.server._writers), len(rig.server._tasks)

    active, per_host, writers, tasks = run(go())
    assert active == 0 and per_host == {} and writers == 0 and tasks == 0


# --------------------------------------------------------------------------
# shutdown and the private directory
# --------------------------------------------------------------------------
def test_the_directory_and_socket_are_private_and_the_bridge_script_is_read_only():
    async def go():
        async with Rig() as rig:
            d = Path(rig.server.directory)
            sock = Path(rig.server.socket_path)
            return (stat.S_IMODE(d.stat().st_mode), d.stat().st_uid, stat.S_IMODE(sock.stat().st_mode),
                    stat.S_IMODE((d / bridge.SCRIPT_NAME).stat().st_mode), (d / bridge.SCRIPT_NAME).read_text(),
                    sorted(p.name for p in d.iterdir()))

    dmode, uid, smode, bmode, text, names = run(go())
    assert dmode == 0o700 and uid == os.geteuid()
    assert smode == 0o600
    assert bmode == 0o444
    assert text == bridge.bridge_source()
    assert names == sorted([bridge.SCRIPT_NAME, bridge.SOCKET_NAME])


def test_shutdown_closes_every_tunnel_and_removes_the_directory_and_is_safe_twice():
    async def go():
        rig = Rig()
        await rig.__aenter__()
        d = rig.server.directory
        tunnels = [await rig.connect("allowed.example:443") for _ in range(3)]
        half = await rig.open()
        half[1].write(b"CONNECT allowed.example:443 HTTP/1.1\r\n")
        await half[1].drain()
        await rig.__aexit__(None, None, None)
        closed = [await asyncio.wait_for(r.read(), 3) == b"" for r, _, _ in tunnels]
        try:
            half_closed = await asyncio.wait_for(half[0].read(), 3)       # hung up on: EOF or a reset, never a wait
        except ConnectionError:
            half_closed = b""
        await rig.server.aclose()                                         # a second close is harmless
        return d, closed, half_closed, rig.server._active, len(rig.server._writers)

    d, closed, half_closed, active, writers = run(go())
    assert all(closed) and half_closed == b""
    assert not os.path.exists(d)
    assert active == 0 and writers == 0


def test_the_socket_stops_accepting_after_shutdown():
    async def go():
        rig = Rig()
        await rig.__aenter__()
        path = rig.server.socket_path
        await rig.__aexit__(None, None, None)
        with pytest.raises((FileNotFoundError, ConnectionRefusedError, OSError)):
            await asyncio.open_unix_connection(path)

    run(go())


def test_a_failed_start_leaves_no_directory_behind(tmp_path, monkeypatch):
    async def go():
        policy = NetworkPolicy()
        server = ProxyServer(policy)
        monkeypatch.setattr(bridge, "bridge_source", lambda: (_ for _ in ()).throw(OSError("disk")))
        with pytest.raises(OSError):
            await server.start()
        return server.directory

    created: list[str] = []
    real = netproxy.make_private_dir

    def spy():
        d = real()
        created.append(d)
        return d

    monkeypatch.setattr(netproxy, "make_private_dir", spy)
    assert run(go()) is None
    assert created and not os.path.exists(created[0])


def test_make_private_dir_is_0700_owned_by_us_and_short_enough_for_a_unix_socket():
    d = netproxy.make_private_dir()
    try:
        st = os.lstat(d)
        assert stat.S_ISDIR(st.st_mode) and stat.S_IMODE(st.st_mode) == 0o700 and st.st_uid == os.geteuid()
        assert len(os.path.join(d, bridge.SOCKET_NAME).encode()) <= 100
        other = netproxy.make_private_dir()
        assert other != d
        os.rmdir(other)
    finally:
        os.rmdir(d)


def test_make_private_dir_refuses_a_temp_location_that_is_too_long_for_a_socket(tmp_path, monkeypatch):
    long_dir = tmp_path / ("x" * 120)
    long_dir.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(long_dir))
    monkeypatch.setenv("TMPDIR", str(long_dir))
    try:
        d = netproxy.make_private_dir()
    except NetworkError as exc:
        assert "/" not in str(exc) and exc.code
    else:                                                  # a fallback location is fine too, if it is short and private
        try:
            assert len(os.path.join(d, bridge.SOCKET_NAME).encode()) <= 100
            assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
        finally:
            os.rmdir(d)


# --------------------------------------------------------------------------
# what is recorded and logged
# --------------------------------------------------------------------------
def test_nothing_about_a_request_beyond_host_decision_and_scope_is_recorded_or_logged(caplog):
    caplog.set_level(logging.DEBUG)

    async def go():
        secret = "s" + secrets.token_hex(10)
        async with Rig(ports=(80, 443), config=ProxyConfig(allow_cleartext=True)) as rig:
            reader, writer, _ = await rig.connect("allowed.example:443", extra=secret.encode())
            await asyncio.wait_for(reader.read(len(secret)), 5)
            writer.close()
            await rig.ask(f"GET http://allowed.example/{secret}?token={secret} HTTP/1.1\r\nCookie: {secret}\r\n\r\n".encode())
            await rig.ask(f"CONNECT unlisted.example:443 HTTP/1.1\r\nAuthorization: {secret}\r\n\r\n".encode())
            return secret, rig.records, rig.server.stats

    secret, records, stats = run(go())
    assert all(set(r) == {"host", "decision", "scope"} for r in records)
    assert secret not in repr(records) and secret not in caplog.text
    assert set(stats) == {"accepted", "tunnels", "refused", "busy"} and all(isinstance(v, int) for v in stats.values())
    assert "allowed.example" not in caplog.text and "unlisted.example" not in caplog.text


# --------------------------------------------------------------------------
# blocked_address on its own
# --------------------------------------------------------------------------
@pytest.mark.parametrize("ip", [PUBLIC, "8.8.8.8", "1.1.1.1", "2606:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808",
                                "2002:808:808::"])
def test_ordinary_public_addresses_are_not_blocked(ip):
    assert blocked_address(ip) is False


@pytest.mark.parametrize("ip", ["not-an-ip", "", None, 5, b"8.8.8.8", "1.2.3", "127.1", "0x7f000001", "8.8.8.8/32", " 8.8.8.8"])
def test_anything_that_is_not_an_address_is_blocked_fail_closed(ip):
    assert blocked_address(ip) is True


def test_the_loopback_flag_relaxes_loopback_and_nothing_else():
    for ip in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
        assert blocked_address(ip) is True and blocked_address(ip, allow_loopback=True) is False
    for ip in ("10.0.0.1", "169.254.169.254", "100.64.0.1", "fe80::1", "fc00::1", "0.0.0.0", "224.0.0.1"):
        assert blocked_address(ip, allow_loopback=True) is True


# --------------------------------------------------------------------------
# the test-only loopback flag is not reachable
# --------------------------------------------------------------------------
def test_loopback_is_refused_by_default_even_for_an_allowlisted_name():
    async def go():
        async with Rig(hosts={"allowed.example": ["127.0.0.1"]}) as rig:
            return await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n"), rig.connects

    out, connects = run(go())
    assert out.startswith(reply_text("destination")) and connects == []


def test_the_flag_is_not_reachable_from_environment_variables(monkeypatch):
    for name in ("M2C_TEST_ALLOW_LOOPBACK", "M2C_ALLOW_LOOPBACK", "ALLOW_LOOPBACK", "M2C_NETWORK_ALLOW_LOOPBACK",
                 "M2C_TEST_SANDBOX", "M2C_NET_TEST", "_TEST_ALLOW_LOOPBACK", "M2C_DEBUG"):
        monkeypatch.setenv(name, "1")

    async def go():
        async with Rig(hosts={"allowed.example": ["127.0.0.1"]}) as rig:
            return await rig.ask(b"CONNECT allowed.example:443 HTTP/1.1\r\n\r\n"), rig.connects

    out, connects = run(go())
    assert out.startswith(reply_text("destination")) and connects == []


def test_the_production_constructors_have_no_way_to_set_the_flag():
    import inspect

    assert "_test_allow_loopback" not in inspect.signature(netproxy.make_service).parameters
    svc = netproxy.make_service(allow=("allowed.example",))
    assert svc._server._allow_loopback is False
    assert not any("loopback" in name.lower() for name in inspect.signature(netproxy.make_service).parameters)


def test_the_flag_name_appears_only_where_it_is_defined_and_in_the_probe():
    src = Path(netproxy.__file__).resolve().parents[1]
    users = sorted(
        str(p.relative_to(src)) for p in src.rglob("*.py") if "_test_allow_loopback" in p.read_text(encoding="utf-8"))
    assert users == ["sandbox/netproxy.py", "sandbox/probe.py"]
    text = Path(netproxy.__file__).read_text(encoding="utf-8")
    for line in text.splitlines():
        if "_test_allow_loopback" in line:
            assert "environ" not in line and "getenv" not in line and "settings" not in line.lower()


def test_neither_the_shell_config_nor_the_settings_can_carry_the_flag():
    from dataclasses import fields

    from m2c.tools.shell import ShellConfig

    assert not [f.name for f in fields(ShellConfig) if "loopback" in f.name.lower()]
    with pytest.raises(TypeError):
        ShellConfig(_test_allow_loopback=True)       # type: ignore[call-arg]
