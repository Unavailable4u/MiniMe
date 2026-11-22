"""
m2c/tests/sandbox/test_netproxy_address_table.py -- Patch SX-04: the shared address table, against m2c's own guard.

backend/utils/url_guard.py (Patch SX-04) carries a COPY of this package's address blocklist
(m2c/src/m2c/sandbox/netproxy.py::blocked_address).  The two copies must agree, so the same table of addresses is
asserted here and in backend/tests/unit/test_url_guard.py.  This file changes no behaviour: it only records what
``blocked_address`` already answers for each row.  If a row ever fails, either this guard or the backend copy has
drifted -- decide which one is right before changing a test.

Nothing here touches the network: ``blocked_address`` only parses text.

Place this file at: m2c/tests/sandbox/test_netproxy_address_table.py
"""
from __future__ import annotations

import pytest

from m2c.sandbox.netproxy import blocked_address

BLOCKED = [
    "127.0.0.1", "10.1.2.3", "169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192", "168.63.129.16",
    "198.18.0.1", "224.0.0.1", "255.255.255.255", "::", "::1", "::ffff:10.0.0.1", "64:ff9b::a9fe:a9fe",
    "2002:a9fe:a9fe::1", "2001::1", "fe80::1", "fc00::1", "fd00:ec2::254", "not-an-ip", "fe80::1%eth0",
]
ALLOWED = ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "64:ff9b::808:808"]  # the last is NAT64 of 8.8.8.8


@pytest.mark.parametrize("ip", BLOCKED)
def test_shared_table_blocked(ip):
    assert blocked_address(ip) is True


@pytest.mark.parametrize("ip", ALLOWED)
def test_shared_table_allowed(ip):
    assert blocked_address(ip) is False
