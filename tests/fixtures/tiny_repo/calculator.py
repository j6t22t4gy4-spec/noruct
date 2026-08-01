def safe_divide(numerator: float, denominator: float) -> float | None:
    """Return None when division cannot be performed."""
    if denominator < 0:
        return None
    return numerator / denominator
