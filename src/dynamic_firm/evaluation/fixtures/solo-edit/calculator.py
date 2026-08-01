def safe_divide(numerator: float, denominator: float) -> float | None:
    """Return None only when the denominator is zero."""
    if denominator < 0:
        return None
    return numerator / denominator
