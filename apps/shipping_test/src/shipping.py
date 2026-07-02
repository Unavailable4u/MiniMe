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