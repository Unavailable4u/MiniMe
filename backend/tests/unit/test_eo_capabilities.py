"""
tests/unit/test_eo_capabilities.py — Patch B0.

eo/capabilities.py is a pure pass-through layer over eo/skill_library.py
and eo/mcp_registry.py in this patch — no new behavior. The one
property worth pinning down: each capabilities.py function returns
exactly what the underlying skill_library/mcp_registry function
returns, unchanged, and calls it with the same arguments. Later
patches (B1, B3, B4) add real new behavior on top of this module; this
test file only covers the B0 baseline.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from eo import capabilities


def test_list_skills_delegates_to_skill_library(monkeypatch):
    fake_skills = {"skill_1": {"title": "Example", "doc": "..."}}
    fake = MagicMock(return_value=fake_skills)
    monkeypatch.setattr(capabilities, "_list_skills", fake)

    result = capabilities.list_skills()

    assert result is fake_skills
    fake.assert_called_once_with()


def test_get_skill_delegates_to_skill_library(monkeypatch):
    fake_entry = {"title": "Example", "doc": "..."}
    fake = MagicMock(return_value=fake_entry)
    monkeypatch.setattr(capabilities, "_get_skill", fake)

    result = capabilities.get_skill("skill_1")

    assert result is fake_entry
    fake.assert_called_once_with("skill_1")


def test_get_skill_passes_through_none_for_missing_id(monkeypatch):
    fake = MagicMock(return_value=None)
    monkeypatch.setattr(capabilities, "_get_skill", fake)

    assert capabilities.get_skill("does-not-exist") is None


def test_list_mcp_servers_delegates_to_mcp_registry(monkeypatch):
    fake_servers = [{"name": "github", "connected": True}]
    fake = MagicMock(return_value=fake_servers)
    monkeypatch.setattr(capabilities, "_list_mcp_servers", fake)

    result = capabilities.list_mcp_servers()

    assert result is fake_servers
    fake.assert_called_once_with()


@pytest.mark.asyncio
async def test_mcp_server_status_delegates_to_mcp_registry(monkeypatch):
    fake_status = {"name": "github", "connected": True, "tools": []}
    fake = AsyncMock(return_value=fake_status)
    monkeypatch.setattr(capabilities, "_mcp_server_status", fake)

    result = await capabilities.mcp_server_status("github")

    assert result is fake_status
    fake.assert_called_once_with("github")


@pytest.mark.asyncio
async def test_mcp_server_status_passes_through_error_shape_for_unknown_server(monkeypatch):
    fake = AsyncMock(return_value={"error": "unknown server"})
    monkeypatch.setattr(capabilities, "_mcp_server_status", fake)

    result = await capabilities.mcp_server_status("does-not-exist")

    assert result == {"error": "unknown server"}


def test_list_capabilities_delegates_to_capability_entries_as_capability_type_only(monkeypatch):
    fake_entries = [{"entry_id": "frontend_renderable_tabs", "entry_type": "capability"}]
    fake = MagicMock(return_value=fake_entries)
    monkeypatch.setattr(capabilities, "_list_capability_entries", fake)

    result = capabilities.list_capabilities(tags=["frontend_capabilities"])

    assert result is fake_entries
    fake.assert_called_once_with(entry_type="capability", tags=["frontend_capabilities"])


def test_list_capabilities_defaults_tags_to_none(monkeypatch):
    fake = MagicMock(return_value=[])
    monkeypatch.setattr(capabilities, "_list_capability_entries", fake)

    capabilities.list_capabilities()

    fake.assert_called_once_with(entry_type="capability", tags=None)
