"""
test_registry.py — Part 5 checklist item 1
Run with: python3 test_registry.py
(or: pytest test_registry.py -v, if you have pytest)
"""
from eo.registry import resolve_role, ROLE_TO_AGENT, resolve

def test_known_roles_resolve_to_expected_agents():
    expected = {
        "implementer": "code_writers",
        "verifier": "reviewer",
        "fixer": "fixer_pool",
        "researcher": "responder",
        "writer": "responder",
        "fact_checker": "reviewer",
        "analyst": "responder",
        "formatter": "responder",
        "editor": "reviewer",
        "security_reviewer": "security_scanner",
        "documentation_writer": "documentation_agent",
    }
    for role, agent_name in expected.items():
        result = resolve_role(role)
        assert result == agent_name, f"{role} -> {result}, expected {agent_name}"
        # bonus: confirm the resolved name is actually a real, callable agent
        resolve(result)  # should not raise
    print("PASS: all known roles resolve correctly")

def test_unmapped_role_raises_keyerror():
    try:
        resolve_role("nonexistent_role_xyz")
    except KeyError:
        print("PASS: unmapped role raised KeyError as expected")
        return
    except Exception as e:
        raise AssertionError(f"FAIL: raised {type(e).__name__} instead of KeyError")
    raise AssertionError("FAIL: resolve_role() returned instead of raising KeyError")

if __name__ == "__main__":
    test_known_roles_resolve_to_expected_agents()
    test_unmapped_role_raises_keyerror()
    print("\nAll Part 5 checklist item 1 tests passed.")