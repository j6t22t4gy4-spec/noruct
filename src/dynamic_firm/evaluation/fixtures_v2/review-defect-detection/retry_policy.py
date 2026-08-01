def backoff_delay(attempt: int, base: int, cap: int) -> int:
    if attempt < 0:
        raise ValueError("attempt must be non-negative")
    return min(cap, base * 2**attempt)
