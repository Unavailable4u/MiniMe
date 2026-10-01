"""
daemon/tests/test_minime_daemon_preview.py — W7.4: how minime_daemon.py
starts the preview proxy. The proxy's own behaviour is covered in
test_preview_proxy.py; this is only the wiring -- in particular that a
proxy which can't start never takes the daemon down with it.

Run: pytest daemon/tests/test_minime_daemon_preview.py -v
"""
import asyncio
import logging
from pathlib import Path

from daemon.config import DaemonConfig
from daemon.minime_daemon import _start_preview_proxy
from daemon.preview_proxy import PreviewProxyConfig


def _config(preview):
    return DaemonConfig(
        pairing_token="a" * 32,
        allowed_root=Path("/tmp"),
        backend_ws_url="ws://localhost:8000",
        workspace_id="w",
        preview=preview,
    )


def _preview(port):
    return PreviewProxyConfig("http", "localhost", 5173, "http://localhost:3000", port=port)


def test_no_preview_settings_means_no_proxy():
    assert asyncio.run(_start_preview_proxy(_config(None))) is None


def test_configured_proxy_starts_listens_and_stops():
    async def scenario():
        proxy = await _start_preview_proxy(_config(_preview(0)))
        assert proxy is not None
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)  # it is really listening
            writer.close()
        finally:
            await proxy.stop()
        return proxy.url

    assert asyncio.run(scenario()).startswith("http://127.0.0.1:")


def test_a_busy_port_is_logged_and_survived(caplog):
    async def scenario():
        blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        busy_port = blocker.sockets[0].getsockname()[1]
        try:
            return await _start_preview_proxy(_config(_preview(busy_port))), busy_port
        finally:
            blocker.close()
            await blocker.wait_closed()

    with caplog.at_level(logging.ERROR, logger="minime_daemon"):
        proxy, busy_port = asyncio.run(scenario())

    assert proxy is None  # no exception: the daemon carries on without the proxy
    assert any("continuing without it" in r.message and str(busy_port) in r.message for r in caplog.records)
