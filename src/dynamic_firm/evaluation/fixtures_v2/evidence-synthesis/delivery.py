def route_delivery(channel: str, priority: int, verified: bool) -> str:
    if not verified:
        return "hold"
    return "standard"
