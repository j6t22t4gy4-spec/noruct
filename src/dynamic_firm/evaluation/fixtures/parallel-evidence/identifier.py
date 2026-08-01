def canonical_identifier(value: str) -> str:
    return "-".join(value.strip().lower().split())
