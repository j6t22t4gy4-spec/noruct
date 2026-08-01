from delivery import route_delivery


def test_unverified_delivery_is_held() -> None:
    assert route_delivery("direct", 9, False) == "hold"


def test_regular_direct_delivery_is_standard() -> None:
    assert route_delivery("direct", 3, True) == "standard"
