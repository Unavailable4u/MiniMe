def calculate_shipping_cost(weight_kg: float, destination_zone: str) -> float:
    zone_rates = {"domestic": 2.50, "regional": 5.00, "international": 12.00}
    if destination_zone not in zone_rates:
        raise ValueError(f"Unknown zone: {destination_zone}")
    if weight_kg <= 0:
        raise ValueError("Weight must be positive")
    base_rate = zone_rates[destination_zone]
    if weight_kg > 5:
        return base_rate * 5 + (weight_kg - 5) * 1.75
    return base_rate * weight_kg

# --- Generated tests (Test Writer) ---
assert calculate_shipping_cost(2.0, 'domestic') == 5.0
assert calculate_shipping_cost(5.0, 'domestic') == 12.5
assert calculate_shipping_cost(7.0, 'domestic') == 16.0
assert calculate_shipping_cost(5.0, 'regional') == 25.0
assert calculate_shipping_cost(6.0, 'regional') == 26.75
assert calculate_shipping_cost(2.0, 'international') == 24.0
assert calculate_shipping_cost(10.0, 'international') == 68.75

try:
    calculate_shipping_cost(3.0, 'continental')
except ValueError as e:
    assert str(e) == "Unknown zone: continental"

try:
    calculate_shipping_cost(0, 'domestic')
except ValueError as e:
    assert str(e) == "Weight must be positive"

try:
    calculate_shipping_cost(-1.5, 'regional')
except ValueError as e:
    assert str(e) == "Weight must be positive"