"""Shared mock catalog used by router bootstrap and mock self-registration."""
from __future__ import annotations

from intelliroute.common.config import settings
from intelliroute.common.mock_provider_catalog import list_mock_provider_infos_from_settings
from intelliroute.router.main import _mock_bootstrap


def test_catalog_matches_router_mock_bootstrap():
    expected = list_mock_provider_infos_from_settings(settings)
    got = _mock_bootstrap()
    assert [p.model_dump() for p in expected] == [p.model_dump() for p in got]
