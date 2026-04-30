from __future__ import annotations

from intelliroute.cost_tracker.accounting import CostAccountant
from intelliroute.common.models import CostEvent
from intelliroute.rate_limiter.token_bucket import BucketConfig, RateLimiterStore
from intelliroute.eval_harness.runner import _reset_for_run


def test_cost_accountant_reset_clears_rollups_and_budgets() -> None:
    accountant = CostAccountant()
    accountant.set_budget("t1", 1.0)
    accountant.record(
        CostEvent(
            request_id="r1",
            tenant_id="t1",
            team_id="team-a",
            workflow_id="wf-a",
            provider="mock-fast",
            model="fast-1",
            prompt_tokens=10,
            completion_tokens=10,
            estimated_cost_usd=0.01,
            unix_ts=1.0,
        )
    )
    assert accountant.summary("t1").total_requests == 1
    accountant.reset(clear_budgets=True)
    assert accountant.summary("t1").total_requests == 0
    assert accountant.get_budget("t1") is None


def test_rate_limiter_store_reset_clears_state() -> None:
    store = RateLimiterStore(default_config=BucketConfig(capacity=10, refill_rate=1.0))
    store.set_config("tenant|provider", BucketConfig(capacity=1, refill_rate=0.1))
    store.try_consume("tenant|provider", amount=1.0)
    assert store.log_length() == 1
    store.reset(clear_configs=True, clear_log=True)
    assert store.log_length() == 0
    cfg, src = store.resolve_config("tenant|provider")
    assert src == "*"
    assert cfg.capacity == 10


class _FakeResponse:
    def __init__(self, status_code: int = 200) -> None:
        self.status_code = status_code


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> _FakeResponse:
        self.calls.append((url, json or {}))
        return _FakeResponse(200)


def test_runner_reset_calls_services_in_order(monkeypatch) -> None:
    monkeypatch.setenv(
        "INTELLIROUTE_MOCK_PROVIDER_ADMIN_URLS",
        "http://127.0.0.1:9001/admin/force_fail,http://127.0.0.1:9002/admin/force_fail",
    )
    client = _FakeClient()
    _reset_for_run(
        client=client,  # type: ignore[arg-type]
        router_url="http://r",
        cost_tracker_url="http://c",
        health_monitor_url="http://h",
        rate_limiter_urls=["http://l1", "http://l2"],
        run_log=[],
    )
    urls = [u for u, _ in client.calls]
    assert urls[0].endswith("/admin/force_fail")
    assert urls[1].endswith("/admin/force_fail")
    assert "http://r/reset" in urls
    assert "http://c/reset" in urls
    assert "http://h/reset" in urls
    assert "http://l1/reset" in urls
    assert "http://l2/reset" in urls
