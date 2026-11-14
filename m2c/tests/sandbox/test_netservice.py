"""
m2c/tests/sandbox/test_netservice.py -- Patch C11b: the proxy on its own thread (``NetworkService``) and the per-session
registry (``service_for`` / ``close_network``) that the shell tool and the terminal use.

Place this file at: m2c/tests/sandbox/test_netservice.py
"""
from __future__ import annotations

import asyncio
import gc
import os
import socket
import threading
import weakref

import pytest

from m2c.sandbox import netproxy
from m2c.sandbox.netpolicy import NetworkPolicy
from m2c.sandbox.netproxy import NetworkError, NetworkService, close_network, clear_label, service_for


class Owner:
    """Something with identity, like a ToolContext."""


def make(**kw):
    return NetworkService(NetworkPolicy(allow=["ok.example"]), **kw)


def test_start_serves_a_socket_and_close_removes_everything():
    service = make()
    service.start()
    try:
        assert service.alive
        directory = service.directory
        assert os.path.isdir(directory) and os.path.exists(service.socket_path)
        s = socket.socket(socket.AF_UNIX)
        s.connect(service.socket_path)
        s.close()
    finally:
        service.close()
    assert not service.alive and not os.path.exists(directory)
    assert service.directory is None and service.socket_path is None


def test_close_is_idempotent_and_start_twice_is_harmless():
    service = make()
    service.start()
    service.start()
    service.close()
    service.close()
    assert not service.alive


def test_the_proxy_thread_is_named_and_a_daemon_and_ends_on_close():
    service = make()
    service.start()
    threads = [t for t in threading.enumerate() if t.name == "m2c-net-proxy"]
    assert threads and all(t.daemon for t in threads)
    service.close()
    assert not any(t.name == "m2c-net-proxy" and t.is_alive() for t in threading.enumerate() if t in threads)


def test_mount_gives_the_directory_and_python_and_refuses_when_not_running():
    service = make()
    with pytest.raises(NetworkError):
        service.mount("/usr/bin/python3")
    service.start()
    mount = service.mount("/usr/bin/python3")
    assert mount.directory == service.directory and mount.python == "/usr/bin/python3"
    service.close()
    with pytest.raises(NetworkError):
        service.mount("/usr/bin/python3")


def test_a_start_that_fails_raises_and_leaves_nothing(monkeypatch):
    def broken():
        raise OSError("no space")

    monkeypatch.setattr(netproxy, "make_private_dir", broken)
    service = make()
    with pytest.raises(NetworkError) as info:
        service.start()
    assert info.value.code == "start_failed" and not service.alive


def test_run_executes_on_the_proxys_loop_and_times_out():
    service = make()
    service.start()
    try:
        assert service.run(asyncio.sleep(0, result=7)) == 7
        thread_name = service.run(_thread_name())
        assert thread_name == "m2c-net-proxy"
        with pytest.raises(Exception):
            service.run(asyncio.sleep(5), timeout=0.1)
    finally:
        service.close()


async def _thread_name():
    return threading.current_thread().name


@pytest.mark.filterwarnings("ignore:coroutine .* was never awaited:RuntimeWarning")
def test_run_before_start_is_an_error():
    with pytest.raises(NetworkError):
        make().run(asyncio.sleep(0))


def test_the_label_is_set_and_only_ever_a_string():
    service = make()
    service.set_label("pip install x")
    assert service._label == "pip install x"
    service.set_label(None)                                     # type: ignore[arg-type]
    assert service._label == ""


def test_unprompted_denials_are_visible_through_the_service():
    service = make()
    service.start()
    try:
        assert service.unprompted_denials == 0
        service.run(service.policy.decide("unlisted.example", 443))
        assert service.unprompted_denials == 1
    finally:
        service.close()


def test_stats_are_plain_counts():
    service = make()
    service.start()
    try:
        assert service.stats == {"accepted": 0, "tunnels": 0, "refused": 0, "busy": 0}
        stats = service.stats
        stats["accepted"] = 99
        assert service.stats["accepted"] == 0                   # a copy
    finally:
        service.close()


# --------------------------------------------------------------------------
# the registry
# --------------------------------------------------------------------------
def test_service_for_starts_lazily_and_reuses_for_the_same_key():
    owner = Owner()
    built = []

    def build():
        built.append(1)
        return make()

    try:
        first = service_for(owner, "k", build)
        again = service_for(owner, "k", build)
        assert first is again and len(built) == 1 and first.alive and first.key == "k"
    finally:
        close_network(owner)


def test_a_different_key_rebuilds_and_closes_the_old_one():
    owner = Owner()
    try:
        first = service_for(owner, "a", make)
        old_dir = first.directory
        second = service_for(owner, "b", make)
        assert second is not first and not first.alive and not os.path.exists(old_dir) and second.alive
    finally:
        close_network(owner)


def test_a_dead_service_is_replaced():
    owner = Owner()
    try:
        first = service_for(owner, "a", make)
        first.close()
        second = service_for(owner, "a", make)
        assert second is not first and second.alive
    finally:
        close_network(owner)


def test_owners_are_independent():
    a, b = Owner(), Owner()
    try:
        sa, sb = service_for(a, "k", make), service_for(b, "k", make)
        assert sa is not sb and sa.directory != sb.directory
        close_network(a)
        assert not sa.alive and sb.alive
    finally:
        close_network(a)
        close_network(b)


def test_a_failed_build_raises_and_registers_nothing():
    owner = Owner()

    def broken():
        raise NetworkError("start_failed")

    with pytest.raises(NetworkError):
        service_for(owner, "k", broken)
    assert netproxy._SERVICES.get(owner) is None


def test_close_network_and_clear_label_never_raise_and_never_start_anything():
    owner = Owner()
    close_network(owner)
    clear_label(owner)
    assert netproxy._SERVICES.get(owner) is None
    try:
        service = service_for(owner, "k", make)
        service.set_label("x")
        clear_label(owner)
        assert service._label == ""
    finally:
        close_network(owner)


def test_a_service_is_closed_when_its_owner_is_garbage_collected():
    owner = Owner()
    service = service_for(owner, "k", make)
    directory = service.directory
    ref = weakref.ref(owner)
    del owner
    gc.collect()
    assert ref() is None
    assert not service.alive and not os.path.exists(directory)


def test_the_atexit_hook_closes_every_service():
    a, b = Owner(), Owner()
    sa, sb = service_for(a, "k", make), service_for(b, "k", make)
    netproxy._close_all()
    assert not sa.alive and not sb.alive and netproxy._SERVICES.get(a) is None


def test_make_service_builds_the_production_policy(tmp_path):
    from m2c.sandbox.netpolicy import AllowFile

    AllowFile().add("saved.example")
    records = []
    service = netproxy.make_service(allow=("given.example",), ports=(443, 8443), record=records.append, allow_cleartext=True)
    assert service.policy.check("saved.example", 443).action == "allow"
    assert service.policy.check("given.example", 8443).action == "allow"
    assert service.policy.check("given.example", 80).action == "deny"
    assert service._server.config.allow_cleartext is True
    assert service._server._allow_loopback is False
