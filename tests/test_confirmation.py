from app.guardrails.confirmation import requires_confirmation


def test_confirmation_required_for_start_stop() -> None:
    assert requires_confirmation("start", False) is True
    assert requires_confirmation("stop", False) is True


def test_confirmation_not_required_when_confirmed() -> None:
    assert requires_confirmation("start", True) is False


def test_confirmation_not_required_for_query() -> None:
    assert requires_confirmation("query", False) is False
