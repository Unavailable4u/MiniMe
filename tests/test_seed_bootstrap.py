import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from memory.bus import read, write
from eo.registry import get_role_prompt, ROLE_PROMPTS_KEY, ROLE_PROMPTS_SEED

# Force a "fresh" state for this one key only.
write(ROLE_PROMPTS_KEY, None)
print("Cleared registry:role_prompts. Current raw value:", read(ROLE_PROMPTS_KEY))

# First call — should bootstrap from the seed and write it back.
result1 = get_role_prompt("implementer")
print("First call (should bootstrap):", result1)

raw_after_first = read(ROLE_PROMPTS_KEY)
print("Raw store after first call has", len(raw_after_first), "entries:", sorted(raw_after_first.keys()))

# Second call — should just read what's there now, no reseed.
result2 = get_role_prompt("implementer")
print("Second call (should just read):", result2)

assert result1 == result2 == ROLE_PROMPTS_SEED["implementer"]
assert sorted(raw_after_first.keys()) == sorted(ROLE_PROMPTS_SEED.keys())
print("PASS: bootstrapped correctly and matches ROLE_PROMPTS_SEED.")