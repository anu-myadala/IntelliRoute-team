from __future__ import annotations

from intelliroute.common.models import CompletionRequest


def test_completion_request_accepts_optional_team_and_workflow_ids():
    req = CompletionRequest(
        tenant_id="t1",
        team_id="team-a",
        workflow_id="wf-1",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert req.team_id == "team-a"
    assert req.workflow_id == "wf-1"


def test_completion_request_backwards_compatible_without_team_workflow():
    req = CompletionRequest(
        tenant_id="t1",
        messages=[{"role": "user", "content": "hello"}],
    )
    assert req.team_id is None
    assert req.workflow_id is None
