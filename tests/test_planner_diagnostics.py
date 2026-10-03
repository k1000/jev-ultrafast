"""Safe diagnostic codes, never provider/page/credential data; no network or browser."""

import json
from unittest.mock import Mock

import pytest

from jev_ultrafast import planning


@pytest.fixture
def post(monkeypatch):
    monkeypatch.setenv("PLANNER_API_KEY", "secret-api-key")
    monkeypatch.setenv("PLANNER_MODEL", "test-model")
    monkeypatch.setenv("PLANNER_BASE_URL", "https://planner.test/v1")
    mock = Mock()
    monkeypatch.setattr(planning, "post_json", mock)
    return mock


def response(arguments):
    return {"choices": [{"message": {"tool_calls": [{"type": "function", "function": {
        "name": "prepare_plan", "arguments": arguments,
    }}]}}]}


def test_missing_configuration_is_identified_before_transport(monkeypatch, post):
    monkeypatch.delenv("PLANNER_API_KEY")
    with pytest.raises(planning.PlanningError) as caught:
        planning.plan_objective({"objective": "Original"})
    assert caught.value.diagnostic == {"stage": "configuration", "code": "missing_configuration",
                                       "transport_attempted": False, "missing": ["PLANNER_API_KEY"]}
    post.assert_not_called()


@pytest.mark.parametrize("case,stage,code", [
    ("transport", "transport", "request_failed"),
    ("envelope", "envelope", "invalid_tool_calls"),
    ("arguments", "arguments", "invalid_json"),
    ("objective", "plan_validation", "objective_mismatch"),
    ("checks", "plan_validation", "invalid_check_binding"),
])
def test_failure_stage_is_safe_and_no_second_request_is_made(post, case, stage, code):
    data = {"objective": "Original", "plan": []}
    if case == "transport":
        post.side_effect = RuntimeError("secret-api-key and private page data")
    elif case == "envelope":
        post.return_value = {"choices": [{"message": {"content": "private provider data"}}]}
    elif case == "arguments":
        post.return_value = response("{private page data")
    else:
        if case == "objective":
            data["objective"] = "private altered task"
        else:
            data["plan"] = [{"goal": "Enter private value", "texts": [], "checks": [
                {"kind": "value", "value": "private value", "label": None},
            ]}]
        post.return_value = response(json.dumps(data))
    with pytest.raises(planning.PlanningError) as caught:
        planning.plan_objective({"objective": "Original"})
    diagnostic = caught.value.diagnostic
    assert diagnostic["stage"] == stage and diagnostic["code"] == code
    assert diagnostic["transport_attempted"] is True
    assert "secret" not in str(caught.value) + str(diagnostic)
    assert "private" not in str(caught.value) + str(diagnostic)
    if case == "checks":
        assert diagnostic["path"] == "plan[0].checks[0]"
    post.assert_called_once()


@pytest.mark.parametrize("base", ["not-an-endpoint", "https://[invalid", "file:///private/data"])
def test_invalid_endpoint_is_identified_without_transport(monkeypatch, post, base):
    monkeypatch.setenv("PLANNER_BASE_URL", base)
    with pytest.raises(planning.PlanningError) as caught:
        planning.plan_objective({"objective": "Original"})
    assert caught.value.diagnostic == {"stage": "configuration", "code": "invalid_endpoint",
                                       "transport_attempted": False}
    assert base not in str(caught.value)
    post.assert_not_called()


def test_invalid_request_context_is_identified_without_transport(post):
    with pytest.raises(planning.PlanningError) as caught:
        planning.plan_objective({})
    assert caught.value.diagnostic["stage"] == "request"
    assert caught.value.diagnostic["transport_attempted"] is False
    post.assert_not_called()


def test_objective_snapshot_preserves_safe_diagnostic_and_zero_actions(monkeypatch, post):
    from test_objective import FakeBrowser

    from jev_ultrafast import Check, ObjectiveAgent
    from jev_ultrafast import agent as loop

    monkeypatch.delenv("PLANNER_API_KEY")
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    with ObjectiveAgent("https://example.test/", "Original", checks=(Check("text", "missing"),)) as agent:
        state = agent.command()
        assert state["status"] == "needs_attention" and state["stop_reason"] == "planning_failed"
        assert state["planner_calls"][0]["diagnostic"]["stage"] == "configuration"
        assert "latency_ms" in state["planner_calls"][0]
        assert state["history"] == [] and state["decisions"] == []
    post.assert_not_called()
