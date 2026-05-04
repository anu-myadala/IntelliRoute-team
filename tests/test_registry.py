"""Unit tests for the provider registry (service discovery)."""
from __future__ import annotations

from intelliroute.common.models import ProviderInfo
from intelliroute.router.registry import ProviderRegistry


def _p(name: str) -> ProviderInfo:
    return ProviderInfo(name=name, url=f"http://{name}", model=f"{name}-m")


def test_register_and_get():
    reg = ProviderRegistry()
    reg.register(_p("a"))
    assert reg.get("a") is not None
    assert reg.get("missing") is None


def test_deregister():
    reg = ProviderRegistry()
    reg.register(_p("a"))
    reg.deregister("a")
    assert reg.get("a") is None


def test_bulk_register_and_all():
    reg = ProviderRegistry()
    reg.bulk_register([_p("a"), _p("b"), _p("c")])
    names = sorted(p.name for p in reg.all())
    assert names == ["a", "b", "c"]


def test_re_register_overwrites():
    reg = ProviderRegistry()
    reg.register(_p("a"))
    updated = ProviderInfo(name="a", url="http://new", model="a-m2")
    reg.register(updated)
    assert reg.get("a").url == "http://new"


# ---------------------------------------------------------------------------
# Edge-case and boundary tests
# ---------------------------------------------------------------------------

def test_empty_registry_all_returns_empty_list():
    """An empty registry should return an empty list from all(), not raise.

    This guards against implementations that return None or raise KeyError
    when the internal store is uninitialised.
    """
    reg = ProviderRegistry()
    assert reg.all() == []


def test_get_on_empty_registry_returns_none():
    """Querying any name on a fresh registry must return None, not raise."""
    reg = ProviderRegistry()
    assert reg.get("nonexistent") is None


def test_deregister_nonexistent_is_safe_noop():
    """Deregistering a name that was never registered should not raise.

    The caller should not have to guard against KeyError before calling
    deregister — idempotency is a contract of the interface.
    """
    reg = ProviderRegistry()
    reg.deregister("ghost")  # must not raise


def test_bulk_register_empty_list_leaves_registry_empty():
    """Bulk-registering an empty list must not add any entries."""
    reg = ProviderRegistry()
    reg.bulk_register([])
    assert reg.all() == []


def test_register_preserves_capability_dict():
    """A provider's capability scores must survive the round-trip through
    register() and get() without being mutated or dropped."""
    caps = {"code": 0.9, "reasoning": 0.7}
    info = ProviderInfo(name="cap-p", url="http://cap-p", model="cap-m", capability=caps)
    reg = ProviderRegistry()
    reg.register(info)
    assert reg.get("cap-p").capability == caps


def test_deregister_one_leaves_siblings_intact():
    """Removing one provider must not disturb other registered providers.

    Implementations backed by a shared mutable dict could accidentally
    invalidate neighbour entries; this test catches that regression.
    """
    reg = ProviderRegistry()
    reg.bulk_register([_p("x"), _p("y"), _p("z")])
    reg.deregister("x")
    assert reg.get("x") is None
    assert reg.get("y") is not None
    assert reg.get("z") is not None
    # Confirm the count is exactly two survivors.
    assert len(reg.all()) == 2


def test_bulk_and_single_register_coexist():
    """bulk_register and individual register() calls should all be visible via all()."""
    reg = ProviderRegistry()
    reg.bulk_register([_p("a"), _p("b")])
    reg.register(_p("c"))
    names = sorted(p.name for p in reg.all())
    assert names == ["a", "b", "c"]


def test_register_updates_cost_per_1k_tokens():
    """Re-registering the same provider name with a different cost should overwrite
    the previous entry rather than creating a duplicate."""
    reg = ProviderRegistry()
    reg.register(ProviderInfo(name="p", url="http://p", model="m", cost_per_1k_tokens=0.001))
    reg.register(ProviderInfo(name="p", url="http://p", model="m", cost_per_1k_tokens=0.005))
    assert reg.get("p").cost_per_1k_tokens == 0.005
