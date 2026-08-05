"""
tests/unit/test_namespacing.py — rebuilt around FakeRedis (was a
print-and-eyeball script; see the old tests/test_namespacing.py).

Covers memory/bus.py's _namespaced() contract: ordinary keys are scoped
per app_slug so two "projects" never see each other's data, while the
documented exempt prefixes (app_slug itself, project_registry, usage:*,
registry:*, conversation:*, cooldown_until:*, paused_execution:*) are
deliberately shared across every app_slug.
"""
import pytest

from memory.bus import write, read, KEYS


def test_ordinary_key_is_isolated_per_app_slug():
    write(KEYS["app_slug"], "project_a")
    write("current_plan", {"hello": "project_a's plan"})

    write(KEYS["app_slug"], "project_b")
    # project_b never wrote "current_plan" -- must not see project_a's value.
    assert read("current_plan") is None

    write(KEYS["app_slug"], "project_a")
    # Switching back to project_a restores its own value.
    assert read("current_plan") == {"hello": "project_a's plan"}


@pytest.mark.parametrize("key", [
    "project_registry",
    "usage:groq_key_1",
    "registry:role_prompts",
    "conversation:abc123",
    "cooldown_until:groq_key_1",
    "paused_execution:session-xyz",
])
def test_exempt_prefixes_are_shared_across_app_slugs(key):
    write(KEYS["app_slug"], "project_a")
    write(key, "shared_value")

    write(KEYS["app_slug"], "project_b")
    # An exempt key is a property of the system/session/account, not
    # whatever app_slug happens to be active -- must be visible here too.
    assert read(key) == "shared_value"


def test_app_slug_key_itself_is_never_prefixed():
    # app_slug is the bootstrap key -- it can't be namespaced by its own
    # value. Writing it under one slug and reading it back after
    # switching to another slug should still return the raw global value.
    write(KEYS["app_slug"], "project_a")
    write(KEYS["app_slug"], "project_b")
    assert read(KEYS["app_slug"]) == "project_b"


def test_no_app_slug_set_falls_back_to_unprefixed_key():
    # Before any app_slug is ever written, ordinary keys behave as if
    # there's no namespace at all.
    write("some_key", "value_with_no_slug")
    assert read("some_key") == "value_with_no_slug"
