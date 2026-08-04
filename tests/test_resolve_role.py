import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eo.registry import resolve_role, ROLE_TO_AGENT, DEFAULT_FALLBACK_AGENT

UNMAPPED_ROLE = "some_role_nobody_ever_mapped_xyz"
assert UNMAPPED_ROLE not in ROLE_TO_AGENT, "test role accidentally collides with a real mapping"

result = resolve_role(UNMAPPED_ROLE)
print(f"resolve_role('{UNMAPPED_ROLE}') returned: {result!r}")

assert result == DEFAULT_FALLBACK_AGENT == "responder"
print("PASS: unmapped role falls back to 'responder' instead of raising KeyError.")

# Sanity check: a genuinely mapped role still resolves correctly (no regression).
known_result = resolve_role("implementer")
print(f"resolve_role('implementer') returned: {known_result!r}")
assert known_result == "code_writers"
print("PASS: existing mapped roles still resolve correctly, unaffected by the fallback.")