"""Regression: provider ids must join across mock-* and short display names (mirrors admin UI)."""


def _strip_mock(id_: str) -> str:
    s = (id_ or "").strip()
    if s.lower().startswith("mock-"):
        return s[5:]
    return s


def _alias_match(wire: str, registry_name: str) -> bool:
    return _strip_mock(wire) == _strip_mock(registry_name)


def test_mock_cheap_matches_cheap_tier_name() -> None:
    assert _alias_match("mock-cheap", "mock-cheap")
    assert _alias_match("mock-cheap", "cheap") is True
    assert _alias_match("cheap", "mock-cheap") is True


def test_mock_prefix_symmetry() -> None:
    for a, b in (("mock-fast", "fast"), ("mock-smart", "smart")):
        assert _alias_match(a, b)
        assert _alias_match(b, a)
