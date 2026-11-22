"""
tests/unit/test_url_guard.py — Patch SX-04.

Covers utils/url_guard.py: the address blocklist (blocked_address), the text rules (check_url_syntax), the
resolving check (validate_url) and the YouTube host check (validate_youtube_url).

No test here touches the network: every test uses a fake resolver, and an autouse fixture makes the real
``socket.getaddrinfo`` raise, so a test that forgot to use the fake fails loudly instead of doing a real lookup.
Nothing in this file skips.
"""
import dataclasses
import socket
from pathlib import Path

import pytest

from utils import url_guard
from utils.url_guard import (
    DEFAULT_POLICY,
    GuardPolicy,
    ParsedURL,
    UnsafeURL,
    blocked_address,
    check_url_syntax,
    validate_url,
    validate_youtube_url,
)


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a test reached the real DNS resolver")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


class FakeResolver:
    """Maps a host name to the list of addresses a lookup should answer; counts and records the calls."""

    def __init__(self, table=None):
        self.table = dict(table or {})
        self.calls = []

    def __call__(self, host, port):
        self.calls.append((host, port))
        if host not in self.table:
            raise OSError("no such host")
        return list(self.table[host])


def policy_with(table=None, **overrides):
    resolver = FakeResolver(table)
    return resolver, GuardPolicy(resolver=resolver, **overrides)


# --------------------------------------------------------------------------
# blocked_address
# --------------------------------------------------------------------------
BLOCKED = [
    "127.0.0.1", "10.1.2.3", "169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192", "168.63.129.16",
    "198.18.0.1", "224.0.0.1", "255.255.255.255", "::", "::1", "::ffff:10.0.0.1", "64:ff9b::a9fe:a9fe",
    "2002:a9fe:a9fe::1", "2001::1", "fe80::1", "fc00::1", "fd00:ec2::254", "not-an-ip", "fe80::1%eth0",
    # extra rows: range edges, the other documentation ranges, other embedded forms
    "0.0.0.0", "172.16.0.1", "172.31.255.255", "192.168.1.1", "100.64.0.1", "100.127.255.255", "192.0.2.1",
    "198.51.100.1", "203.0.113.1", "192.88.99.1", "240.0.0.1", "::127.0.0.1", "::ffff:127.0.0.1",
    "64:ff9b::7f00:1", "2002:7f00:1::", "2001:db8::1", "3fff::1", "ff02::1", "2606:4700:4700::1111%eth0",
]
ALLOWED = [
    "8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "64:ff9b::808:808",
    # extra rows: just outside the blocked ranges, mapped / 6to4 forms of a public address
    "172.32.0.1", "172.15.255.255", "100.128.0.1", "100.63.255.255", "11.0.0.1", "2001:4860:4860::8888",
    "::ffff:8.8.8.8", "2002:808:808::",
]


@pytest.mark.parametrize("ip", BLOCKED)
def test_blocked_address_true(ip):
    assert blocked_address(ip) is True


@pytest.mark.parametrize("ip", ALLOWED)
def test_blocked_address_false(ip):
    assert blocked_address(ip) is False


@pytest.mark.parametrize("value", ["", " 8.8.8.8", "8.8.8.8 ", "8.8.8.8/32", "127.1", "0x7f000001", "1.2.3", None, 5, b"8.8.8.8"])
def test_blocked_address_fails_closed_for_anything_that_is_not_a_plain_ip(value):
    assert blocked_address(value) is True


# --------------------------------------------------------------------------
# the hostile-URL table (section 8.1 of the patch guide): must be rejected
# --------------------------------------------------------------------------
HOSTILE_URLS = [
    # cloud metadata, loopback names, unspecified, IPv6 forms
    "http://169.254.169.254/latest/meta-data/",
    "http://localhost/", "http://foo.localhost/", "http://LOCALHOST/", "http://localhost./",
    "http://0.0.0.0/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[::127.0.0.1]/",
    "http://[64:ff9b::7f00:1]/", "http://[64:ff9b::a9fe:a9fe]/", "http://[2002:7f00:1::]/",
    "http://[fe80::1%25eth0]/", "http://[fe80::1%eth0]/", "http://[::1%25x]/",
    # private and shared ranges
    "http://10.0.0.1/", "http://172.16.0.1/", "http://192.168.1.1/", "http://100.64.0.1/",
    # numeric spellings of an address
    "http://2130706433/", "http://0x7f000001/", "http://127.1/", "http://0177.0.0.1/",
    "http://1.2.3/", "http://foo.123/", "http://0x7f.0.0.1/",
    # look-alike dots and full-width digits
    "http://127\u30020\u30020\u30021/", "http://\uff11\uff12\uff17\uff0e\uff10\uff0e\uff10\uff0e\uff11/",
    # schemes
    "file:///etc/passwd", "gopher://127.0.0.1/", "ftp://example.com/", "data:text/html,x", "javascript:alert(1)",
    "http:example.com", "//example.com/", "example.com/", "HTTPX://example.com/",
    # user name / password, backslash
    "http://user@example.com/", "http://@example.com/", "http://example.com\\@127.0.0.1/",
    "http://user:pw@example.com/", "http://example.com\uff20127.0.0.1/",
    # ports
    "http://example.com:22/", "http://example.com:8080/", "http://example.com:0/", "http://example.com:99999/",
    "http://example.com:/", "http://example.com:80:80/", "http://example.com:http/", "http://example.com:\u0668\u0660/",
    # injection and bad characters
    "http://example.com/\r\nHost: evil", "http://example.com/a b", "http://exa mple.com/", "http://example.com/\x7f",
    "http://example.com/\x00", "http://example.com/\t", "http://example.com/\u0085", "http://example.com/\u2028",
    "http://example.com/\ud800",
    # other malformed hosts
    "http://", "http:///x", "http://?x", "http://#x", "http://::1/", "http://[127.0.0.1]/", "http://[::1/",
    "http://[::1]x/", "http://foo..bar/", "http://.example.com/", "http://-a.example.com/", "http://a-.example.com/",
    "http://a%2eb.com/", "http://example.com%00/", "http://exa$mple.com/", "http://a" + "b" * 64 + ".com/",
    "http://" + ".".join(["abcdefghij"] * 26) + "/",
    # not text, empty, too long
    "", None, 5, b"http://example.com/", "http://example.com/" + "a" * 2048,
]


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_check_url_syntax_rejects_hostile_urls(url):
    with pytest.raises(UnsafeURL):
        check_url_syntax(url)


@pytest.mark.parametrize("url", HOSTILE_URLS)
def test_validate_url_rejects_hostile_urls_before_any_lookup(url):
    resolver, policy = policy_with({"example.com": ["93.184.216.34"]})
    with pytest.raises(UnsafeURL):
        validate_url(url, policy)
    assert resolver.calls == []


# --------------------------------------------------------------------------
# what is allowed, and what the parsed record holds
# --------------------------------------------------------------------------
def test_ordinary_urls_are_accepted_and_parsed():
    assert check_url_syntax("https://example.com/page?x=1") == ParsedURL("https", "example.com", 443, "/page?x=1", False)
    assert check_url_syntax("http://example.com/") == ParsedURL("http", "example.com", 80, "/", False)
    assert check_url_syntax("http://example.com:80/x") == ParsedURL("http", "example.com", 80, "/x", False)
    assert check_url_syntax("https://example.com:443/") == ParsedURL("https", "example.com", 443, "/", False)
    assert check_url_syntax("http://example.com:443/") == ParsedURL("http", "example.com", 443, "/", False)
    assert check_url_syntax("https://8.8.8.8/x") == ParsedURL("https", "8.8.8.8", 443, "/x", True)
    assert check_url_syntax("https://[2606:4700:4700::1111]/") == ParsedURL("https", "2606:4700:4700::1111", 443, "/", True)


def test_the_request_target_is_never_empty_and_is_percent_encoded():
    assert check_url_syntax("http://example.com").target == "/"
    assert check_url_syntax("http://example.com?x=1").target == "/?x=1"
    assert check_url_syntax("http://example.com#frag").target == "/"
    assert check_url_syntax("http://example.com/a#frag").target == "/a"
    assert check_url_syntax("http://example.com/a%20b/c?q=1&r=2").target == "/a%20b/c?q=1&r=2"
    assert check_url_syntax("http://example.com/caf\u00e9?q=\u00fc").target == "/caf%C3%A9?q=%C3%BC"
    assert check_url_syntax("http://example.com/?").target == "/?"


def test_the_host_is_normalised():
    assert check_url_syntax("HTTP://EXAMPLE.COM./x").host == "example.com"
    assert check_url_syntax("http://b\u00fccher.example/").host == "xn--bcher-kva.example"
    assert check_url_syntax("http://Example_Host.test/").host == "example_host.test"
    assert check_url_syntax("http://example.com/" + "a" * 2000).host == "example.com"        # under the length limit
    assert check_url_syntax("http://" + ".".join(["a" * 63] * 3) + ".com/").is_ip is False    # long but legal labels


def test_a_name_that_only_looks_partly_numeric_is_still_a_name():
    assert check_url_syntax("http://1.2.3.4.example.com/").host == "1.2.3.4.example.com"
    assert check_url_syntax("http://deadbeef.com/").host == "deadbeef.com"


def test_the_parsed_record_is_immutable():
    parsed = check_url_syntax("http://example.com/")
    with pytest.raises(dataclasses.FrozenInstanceError):
        parsed.host = "evil.example"


@pytest.mark.parametrize("url", ["http://user@example.com/", "http://@example.com/", "http://u:p@example.com/"])
def test_userinfo_is_refused_by_its_own_rule(url):
    with pytest.raises(UnsafeURL, match="user name or password"):
        check_url_syntax(url)


def test_unsafe_url_is_a_value_error():
    assert issubclass(UnsafeURL, ValueError)


# --------------------------------------------------------------------------
# validate_url: names are resolved and EVERY answer must pass
# --------------------------------------------------------------------------
def test_a_name_with_a_public_ipv4_answer_is_allowed():
    resolver, policy = policy_with({"example.com": ["93.184.216.34"]})
    parsed = validate_url("https://example.com/page?x=1", policy)
    assert parsed.host == "example.com" and parsed.port == 443 and parsed.target == "/page?x=1"
    assert resolver.calls == [("example.com", 443)]


def test_a_name_with_a_public_ipv6_answer_is_allowed():
    _, policy = policy_with({"example.com": ["2606:2800:220:1:248:1893:25c8:1946"]})
    assert validate_url("https://example.com/", policy).host == "example.com"


def test_port_80_is_allowed_and_is_what_the_resolver_is_asked_for():
    resolver, policy = policy_with({"example.com": ["93.184.216.34"]})
    assert validate_url("http://example.com/", policy).port == 80
    assert resolver.calls == [("example.com", 80)]


def test_a_name_that_resolves_to_loopback_is_rejected():
    _, policy = policy_with({"evil.example": ["127.0.0.1"]})
    with pytest.raises(UnsafeURL):
        validate_url("http://evil.example/", policy)


def test_a_name_with_one_public_and_one_private_answer_is_rejected():
    _, policy = policy_with({"mixed.example": ["93.184.216.34", "10.0.0.5"]})
    with pytest.raises(UnsafeURL):
        validate_url("http://mixed.example/", policy)
    _, policy = policy_with({"mixed.example": ["10.0.0.5", "93.184.216.34"]})
    with pytest.raises(UnsafeURL):
        validate_url("http://mixed.example/", policy)


@pytest.mark.parametrize("answer", [
    "169.254.169.254", "::1", "::ffff:127.0.0.1", "64:ff9b::a9fe:a9fe", "fe80::1%eth0", "not-an-ip", "",
])
def test_any_blocked_or_unparsable_answer_rejects_the_name(answer):
    _, policy = policy_with({"evil.example": ["93.184.216.34", answer]})
    with pytest.raises(UnsafeURL):
        validate_url("https://evil.example/", policy)


def test_a_failed_lookup_is_a_refusal():
    resolver, policy = policy_with({})
    with pytest.raises(UnsafeURL):
        validate_url("https://unknown.example/", policy)
    assert resolver.calls == [("unknown.example", 443)]


def test_an_empty_answer_is_a_refusal():
    _, policy = policy_with({"empty.example": []})
    with pytest.raises(UnsafeURL):
        validate_url("https://empty.example/", policy)


def test_any_exception_from_the_resolver_is_a_refusal():
    def broken(host, port):
        raise RuntimeError("boom")
    with pytest.raises(UnsafeURL):
        validate_url("https://example.com/", GuardPolicy(resolver=broken))


def test_an_ip_literal_is_judged_without_a_lookup():
    resolver, policy = policy_with({})
    assert validate_url("http://8.8.8.8/", policy).is_ip is True
    assert resolver.calls == []


def test_error_messages_never_echo_the_url_or_an_address():
    url = "http://internal-admin.corp.example/secret?token=abc"
    _, policy = policy_with({"internal-admin.corp.example": ["10.9.8.7"]})
    for bad in (url, "http://10.9.8.7/", "http://internal-admin.corp.example:22/", "http://[::1]/"):
        with pytest.raises(UnsafeURL) as info:
            validate_url(bad, policy)
        text = str(info.value)
        for secret in ("10.9.8.7", "internal-admin", "corp.example", "token", "::1"):
            assert secret not in text


def test_the_default_resolver_asks_getaddrinfo_for_stream_sockets_and_returns_address_strings(monkeypatch):
    seen = []

    def fake_getaddrinfo(host, port, **kwargs):
        seen.append((host, port, kwargs))
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port)),
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:2800:220:1::1", port, 0, 0)),
        ]
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert url_guard.default_resolver("example.com", 443) == ["93.184.216.34", "2606:2800:220:1::1"]
    assert seen == [("example.com", 443, {"type": socket.SOCK_STREAM})]
    assert validate_url("https://example.com/").host == "example.com"      # the default policy uses it


# --------------------------------------------------------------------------
# the policy is replaceable
# --------------------------------------------------------------------------
def test_a_policy_can_allow_loopback_for_tests_and_nothing_else():
    allow_loopback = GuardPolicy(blocked=lambda ip: blocked_address(ip) and ip != "127.0.0.1")
    assert check_url_syntax("http://127.0.0.1/", allow_loopback).is_ip is True
    with pytest.raises(UnsafeURL):
        check_url_syntax("http://10.0.0.1/", allow_loopback)
    with pytest.raises(UnsafeURL):
        check_url_syntax("http://127.0.0.1/")                                # the default policy still refuses it


def test_ports_can_be_widened_or_removed():
    with pytest.raises(UnsafeURL):
        check_url_syntax("https://example.com:8443/")
    assert check_url_syntax("https://example.com:8443/", GuardPolicy(ports=frozenset({8443}))).port == 8443
    assert check_url_syntax("https://example.com:22/", GuardPolicy(ports=None)).port == 22
    with pytest.raises(UnsafeURL):
        check_url_syntax("https://example.com:0/", GuardPolicy(ports=None))
    with pytest.raises(UnsafeURL):
        check_url_syntax("https://example.com:65536/", GuardPolicy(ports=None))


def test_the_default_policy_is_frozen_and_has_the_documented_defaults():
    assert DEFAULT_POLICY.ports == frozenset({80, 443})
    assert DEFAULT_POLICY.blocked is blocked_address
    assert DEFAULT_POLICY.resolver is url_guard.default_resolver
    assert DEFAULT_POLICY.ca_certs is None
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_POLICY.ports = None


# --------------------------------------------------------------------------
# validate_youtube_url
# --------------------------------------------------------------------------
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=abc", "https://youtu.be/abc", "https://youtube.com/watch?v=abc",
    "https://m.youtube.com/watch?v=abc", "https://music.youtube.com/watch?v=abc",
    "https://www.youtube-nocookie.com/embed/abc", "https://youtube-nocookie.com/embed/abc",
    "http://www.youtube.com/watch?v=abc", "HTTPS://WWW.YOUTUBE.COM/watch?v=abc",
])
def test_youtube_hosts_pass_and_the_url_is_returned_unchanged(url):
    assert validate_youtube_url(url) == url


@pytest.mark.parametrize("url", [
    "https://youtube.com.evil.com/", "https://evil.com/?u=youtube.com", "https://youtu.be@evil.com/",
    "https://evil.com/youtu.be/", "https://notyoutube.com/", "https://youtube.evil/", "https://www.youtube.com.evil.com/",
    "http://127.0.0.1/", "file:///etc/passwd", "https://www.youtube.com:8443/", "https://www.youtube.com\\@evil.com/",
    "https://www.youtube.com/ x", "ftp://www.youtube.com/", "", None, 5,
])
def test_other_hosts_and_hostile_urls_are_refused(url):
    with pytest.raises(UnsafeURL):
        validate_youtube_url(url)


def test_the_youtube_check_does_no_dns(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the YouTube check must not resolve names")
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    resolver = FakeResolver({"www.youtube.com": ["93.184.216.34"]})
    assert validate_youtube_url("https://www.youtube.com/watch?v=abc", GuardPolicy(resolver=resolver))
    assert resolver.calls == []


# --------------------------------------------------------------------------
# the module is self-contained
# --------------------------------------------------------------------------
def test_the_module_copies_the_m2c_blocklist_and_does_not_import_it():
    source = Path(url_guard.__file__).read_text(encoding="utf-8")
    for line in source.splitlines():
        stripped = line.strip()
        assert not (stripped.startswith(("import ", "from ")) and "m2c" in stripped)
