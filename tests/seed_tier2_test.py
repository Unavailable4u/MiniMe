"""
One-off seed script: sets an active app_slug and drops one module into
submitted_code, so a tier-2 add_tests task has something real to act on.
Run this once, THEN submit the tier-2 task through the frontend/CLI.

Usage:
    python seed_tier2_test.py
"""
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from memory.bus import write, read, KEYS, slugify

APP_SLUG = slugify("tier2 routing test")  # -> "tier2_routing_test"

SHIPPING_MODULE_NAME = "shipping.py"
SHIPPING_CODE = '''def calculate_shipping_cost(weight_kg: float, destination_zone: str) -> float:
    zone_rates = {"domestic": 2.50, "regional": 5.00, "international": 12.00}
    if destination_zone not in zone_rates:
        raise ValueError(f"Unknown zone: {destination_zone}")
    if weight_kg <= 0:
        raise ValueError("Weight must be positive")
    base_rate = zone_rates[destination_zone]
    if weight_kg > 5:
        return base_rate + (weight_kg - 5) * 1.75
    return base_rate * weight_kg
'''

# 1. Set the active app_slug FIRST -- write() special-cases this key (no
#    prefixing) and _namespaced() reads it via the cache, so every write
#    after this point gets prefixed under "tier2_routing_test:..."
write(KEYS["app_slug"], APP_SLUG)

# 2. Now write submitted_code -- this write lands at
#    "tier2_routing_test:submitted_code" because app_slug is now active.
write(KEYS["submitted_code"], {
    SHIPPING_MODULE_NAME: {"language": "python", "code": SHIPPING_CODE}
})

# 3. Sanity read-back, to confirm namespacing actually applied.
confirmed_slug = read(KEYS["app_slug"])
confirmed_code = read(KEYS["submitted_code"])
print(f"app_slug set to: {confirmed_slug!r}")
print(f"submitted_code modules: {list(confirmed_code.keys())}")
print()
print("Now submit the tier-2 task through your frontend/CLI, e.g.:")
print(f'  "Add unit tests for shipping.py using pytest — cover the normal ')
print(f'   cases and edge cases, do not modify the function itself."')
