"""Patch SX-24 -- URL rules for transport=http entries in mcp_servers.json (eo/mcp_registry.py).

Pure config-loading tests against small temp JSON files: no network, no DNS (one test makes
socket.getaddrinfo explode to prove it), no MCP handshake. Nothing here skips.
"""
import json
import socket

import pytest

import eo.mcp_client as mcp_client
from eo.mcp_registry import MCPRegistryError, connect_configured_servers, load_server_configs


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_module_state():
    mcp_client._connections.clear()
    yield
    mcp_client._connections.clear()


def _write_config(tmp_path, servers):
    path = tmp_path / "mcp_servers.json"
    path.write_text(json.dumps({"servers": servers}))
    return str(path)


def _http(url, **extra):
    return dict({"name": "srv", "enabled": True, "transport": "http", "url": url}, **extra)


def _load_one(tmp_path, url, **extra):
    return load_server_configs(_write_config(tmp_path, [_http(url, **extra)]))[0]


# ---- normal (non-local) entries ---------------------------------------------------------------

ACCEPTED = [
    "https://api.githubcopilot.com/mcp/",
    "https://example.com/mcp",
    "https://example.com:8443/mcp",       # any port is fine for MCP
    "https://mcp.example.com:9/x?y=1",
    "https://93.184.216.34/mcp",          # a public IP literal
    "https://[2606:4700:4700::1111]/mcp",
]


@pytest.mark.parametrize("url", ACCEPTED)
def test_valid_https_urls_are_accepted_and_kept_as_written(tmp_path, url):
    cfg = _load_one(tmp_path, url)
    assert cfg.url == url
    assert cfg.local is False


REFUSED = [
    pytest.param("http://example.com/mcp", id="plain-http"),
    pytest.param("https://169.254.169.254/latest/meta-data/", id="cloud-metadata"),
    pytest.param("https://10.0.0.5/mcp", id="private-10"),
    pytest.param("https://192.168.1.1:8443/mcp", id="private-192"),
    pytest.param("https://100.64.0.1/mcp", id="cgnat"),
    pytest.param("https://127.0.0.1/mcp", id="loopback-v4"),
    pytest.param("https://[::1]/mcp", id="loopback-v6"),
    pytest.param("https://[::ffff:127.0.0.1]/mcp", id="mapped-loopback"),
    pytest.param("https://localhost/mcp", id="localhost-name"),
    pytest.param("https://foo.localhost:9000/mcp", id="localhost-subdomain"),
    pytest.param("https://2130706433/mcp", id="numeric-loopback"),
    pytest.param("https://0x7f000001/mcp", id="hex-loopback"),
    pytest.param("https://user@example.com/mcp", id="userinfo"),
    pytest.param("https://user:hunter2@example.com/mcp", id="userinfo-password"),
    pytest.param("https://@example.com/mcp", id="empty-userinfo"),
    pytest.param("file:///etc/passwd", id="file-scheme"),
    pytest.param("ftp://example.com/mcp", id="ftp-scheme"),
    pytest.param("gopher://example.com/", id="gopher-scheme"),
    pytest.param("javascript:alert(1)", id="javascript-scheme"),
    pytest.param("https://example.com/mcp\r\nX-Evil: 1", id="header-injection"),
    pytest.param("https://exa mple.com/mcp", id="space"),
    pytest.param("https://example.com\\@127.0.0.1/", id="backslash"),
    pytest.param("example.com/mcp", id="no-scheme"),
    pytest.param("https://", id="no-host"),
    pytest.param("https://example.com:99999/mcp", id="bad-port"),
]


@pytest.mark.parametrize("url", REFUSED)
def test_bad_urls_are_refused_with_a_registry_error(tmp_path, url):
    with pytest.raises(MCPRegistryError) as exc:
        _load_one(tmp_path, url)
    assert "server 'srv'" in str(exc.value)


def test_plain_http_message_explains_the_local_option(tmp_path):
    with pytest.raises(MCPRegistryError) as exc:
        _load_one(tmp_path, "http://example.com/mcp")
    assert "https://" in str(exc.value)
    assert '"local": true' in str(exc.value)


def test_a_loopback_http_url_is_refused_unless_the_entry_is_local(tmp_path):
    with pytest.raises(MCPRegistryError):
        _load_one(tmp_path, "http://127.0.0.1:9000/mcp")
    assert _load_one(tmp_path, "http://127.0.0.1:9000/mcp", local=True).url == "http://127.0.0.1:9000/mcp"


def test_refusal_messages_never_echo_the_url(tmp_path):
    for url in ("https://user:hunter2@internal.example.com/mcp", "https://10.9.8.7/secret-path"):
        with pytest.raises(MCPRegistryError) as exc:
            _load_one(tmp_path, url)
        text = str(exc.value)
        assert "hunter2" not in text and "internal.example.com" not in text
        assert "10.9.8.7" not in text and "secret-path" not in text


# ---- "local": true ----------------------------------------------------------------------------

LOCAL_ACCEPTED = [
    "http://127.0.0.1:9000/mcp",
    "http://127.0.0.1/mcp",
    "http://127.1.2.3:8080/mcp",          # all of 127.0.0.0/8 is loopback
    "http://[::1]:9000/mcp",
    "https://127.0.0.1:9443/mcp",
]


@pytest.mark.parametrize("url", LOCAL_ACCEPTED)
def test_local_entries_may_use_http_to_a_loopback_ip(tmp_path, url):
    cfg = _load_one(tmp_path, url, local=True)
    assert cfg.url == url
    assert cfg.local is True


LOCAL_REFUSED = [
    pytest.param("http://example.com/mcp", id="public-name"),
    pytest.param("http://93.184.216.34/mcp", id="public-ip"),
    pytest.param("http://10.0.0.5:9000/mcp", id="private-ip"),
    pytest.param("http://169.254.169.254/", id="metadata"),
    pytest.param("http://localhost:9000/mcp", id="localhost-name"),
    pytest.param("http://user@127.0.0.1:9000/mcp", id="userinfo"),
    pytest.param("http://127.0.0.1:9000/mcp\r\nX: y", id="control-chars"),
    pytest.param("http://2130706433:9000/mcp", id="numeric-form"),
    pytest.param("file:///etc/passwd", id="file-scheme"),
]


@pytest.mark.parametrize("url", LOCAL_REFUSED)
def test_local_does_not_open_the_door_to_anything_but_loopback_ips(tmp_path, url):
    with pytest.raises(MCPRegistryError) as exc:
        _load_one(tmp_path, url, local=True)
    assert "loopback IP" in str(exc.value)  # the message says what a local server must use


@pytest.mark.parametrize("bad", ["true", "yes", 1, None, []])
def test_local_must_be_a_real_boolean(tmp_path, bad):
    with pytest.raises(MCPRegistryError, match="'local' must be true or false"):
        _load_one(tmp_path, "http://127.0.0.1:9000/mcp", local=bad)


def test_local_on_a_stdio_entry_is_an_error(tmp_path):
    entry = {"name": "s", "enabled": True, "transport": "stdio", "command": ["x"], "local": True}
    with pytest.raises(MCPRegistryError, match="'local' only applies to transport=http"):
        load_server_configs(_write_config(tmp_path, [entry]))


# ---- environment placeholders -----------------------------------------------------------------

def test_the_url_is_judged_after_placeholders_are_expanded(tmp_path, monkeypatch):
    monkeypatch.setenv("SX24_HOST", "169.254.169.254")
    with pytest.raises(MCPRegistryError):
        _load_one(tmp_path, "https://${SX24_HOST}/mcp")
    monkeypatch.setenv("SX24_HOST", "mcp.example.com")
    assert _load_one(tmp_path, "https://${SX24_HOST}/mcp").url == "https://mcp.example.com/mcp"


def test_a_placeholder_cannot_downgrade_the_scheme(tmp_path, monkeypatch):
    monkeypatch.setenv("SX24_SCHEME", "http")
    with pytest.raises(MCPRegistryError):
        _load_one(tmp_path, "${SX24_SCHEME}://example.com/mcp")


def test_an_unset_placeholder_url_still_loads_and_is_left_for_connect_time(tmp_path, monkeypatch):
    """Existing behaviour: an unset ${VAR} expands to "" and only that server fails later."""
    monkeypatch.delenv("SX24_UNSET", raising=False)
    cfg = _load_one(tmp_path, "${SX24_UNSET}")
    assert cfg.url == ""


@pytest.mark.anyio
async def test_a_server_whose_url_placeholder_is_unset_fails_alone_at_connect(tmp_path, monkeypatch):
    monkeypatch.delenv("SX24_UNSET", raising=False)
    connected = []

    async def fake_connect_server(name, **kwargs):
        connected.append(name)
        if not kwargs["url"]:
            raise mcp_client.MCPClientError("no url")

    monkeypatch.setattr(mcp_client, "connect_server", fake_connect_server)
    path = _write_config(tmp_path, [
        _http("${SX24_UNSET}", name="broken"),
        _http("https://example.com/mcp", name="fine"),
    ])
    results = await connect_configured_servers(path)
    assert results == {"broken": "no url", "fine": None}


# ---- what did NOT change ----------------------------------------------------------------------

def test_loading_does_no_dns_lookup(tmp_path, monkeypatch):
    """The registry is re-read on every classify_tool() call, so loading must stay free of network access."""
    def boom(*args, **kwargs):
        raise AssertionError("DNS lookup while loading the MCP registry")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert _load_one(tmp_path, "https://some-name-that-does-not-exist.example/mcp").transport == "http"


def test_stdio_entries_are_not_affected(tmp_path):
    entry = {"name": "s", "enabled": True, "transport": "stdio", "command": ["npx", "-y", "pkg"]}
    cfg = load_server_configs(_write_config(tmp_path, [entry]))[0]
    assert cfg.command == ["npx", "-y", "pkg"] and cfg.url is None and cfg.local is False


def test_the_shipped_mcp_servers_json_still_loads():
    configs = load_server_configs()  # the real backend/config/mcp_servers.json
    assert {c.name for c in configs} >= {"github", "context7", "web_search"}
    assert all(c.local is False for c in configs)


@pytest.mark.anyio
async def test_a_local_server_is_connected_with_its_http_loopback_url(tmp_path, monkeypatch):
    captured = {}

    async def fake_connect_server(name, **kwargs):
        captured[name] = kwargs

    monkeypatch.setattr(mcp_client, "connect_server", fake_connect_server)
    path = _write_config(tmp_path, [_http("http://127.0.0.1:9000/mcp", name="mine", local=True)])
    assert await connect_configured_servers(path) == {"mine": None}
    assert captured["mine"]["url"] == "http://127.0.0.1:9000/mcp"
    assert captured["mine"]["transport"] == "http"
