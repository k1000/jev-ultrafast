"""Qwen wire contracts: forced structured data only, without browser or paid API calls."""

import json
from unittest.mock import Mock

import pytest

from jev_ultrafast import planning


def test_qwen_forced_function_disables_native_thinking_even_without_reasoning_setting(monkeypatch):
    monkeypatch.setenv("PLANNER_API_KEY", "test-only")
    monkeypatch.setenv("PLANNER_MODEL", "qwen3.8-max")
    monkeypatch.setenv("PLANNER_BASE_URL", "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1")
    monkeypatch.delenv("PLANNER_REASONING", raising=False)
    data = {"objective": "Open the requested article", "plan": []}
    post = Mock(return_value={"choices": [{"finish_reason": "tool_calls", "message": {
        "tool_calls": [{"type": "function", "function": {
            "name": "prepare_plan", "arguments": json.dumps(data),
        }}],
    }}]})
    monkeypatch.setattr(planning, "post_json", post)
    result, meta = planning.plan_objective({"objective": data["objective"]})
    assert result == data and meta["transport"] == "function_calling"
    body = post.call_args.args[2]
    assert body["enable_thinking"] is False
    assert body["parallel_tool_calls"] is False
    assert body["tool_choice"] == {"type": "function", "function": {"name": "prepare_plan"}}
    assert "response_format" not in body


def test_plan_objective_cannot_be_changed_and_plan_array_is_required():
    with pytest.raises(ValueError):
        planning.parse_plan({"objective": "Different task", "plan": []}, objective="Original task")
    with pytest.raises(ValueError):
        planning.parse_plan({"objective": "Original task", "plan": {}}, objective="Original task")
    plan = planning.parse_plan({"objective": "Original task", "plan": []}, objective="Original task")
    assert plan.objective == "Original task" and plan.steps == ()


def test_function_plan_steps_reach_jev_one_at_a_time_with_fresh_state(monkeypatch):
    from test_objective import FakeBrowser, chosen

    from jev_ultrafast import ObjectiveAgent
    from jev_ultrafast import agent as loop
    from jev_ultrafast.planning import Check

    objective = "Search Ada Lovelace and open the article"
    data = {"objective": objective, "plan": [
        {"goal": "Enter the query", "texts": [{"label": "Search", "value": "Ada Lovelace"}],
         "checks": [{"kind": "value", "label": "Search", "value": "Ada Lovelace"}]},
        {"goal": "Open the article", "texts": [],
         "checks": [{"kind": "url", "value": "https://example.test/article"}]},
    ]}
    monkeypatch.setenv("PLANNER_API_KEY", "test-only")
    monkeypatch.setenv("PLANNER_MODEL", "large-planner")
    monkeypatch.setenv("PLANNER_BASE_URL", "https://planner.test/v1")
    post = Mock(return_value={"choices": [{"message": {"tool_calls": [{"type": "function", "function": {
        "name": "prepare_plan", "arguments": json.dumps(data),
    }}]}}]})
    monkeypatch.setattr(planning, "post_json", post)
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    choices = Mock(side_effect=[chosen("e1"), chosen("e2", "CLICK")])
    monkeypatch.setattr(loop, "choose", choices)
    with ObjectiveAgent("https://example.test/", objective,
                        checks=(Check("url", "https://example.test/article"),)) as agent:
        states = list(agent.run())
        assert states[0]["plan_index"] == 1 and states[0]["status"] == "ready"
        assert states[-1]["status"] == "done" and states[-1]["goal"] == objective
        assert states[-1]["plan_index"] == 2
        assert states[-1]["verification"] == [True] and states[-1]["text_calls"] == []
    assert [call.args[1] for call in choices.call_args_list] == ["Enter the query", "Open the article"]
    assert all(call.kwargs["execution"]["objective"] == objective for call in choices.call_args_list)
    assert choices.call_args_list[1].args[0]["actions"][0]["value"] == "Ada Lovelace"
    post.assert_called_once()


def test_verified_progress_during_pre_input_staleness_does_not_spend_recovery_budget(monkeypatch):
    from test_objective import FakeBrowser, chosen

    from jev_ultrafast import ObjectiveAgent
    from jev_ultrafast import agent as loop
    from jev_ultrafast.browser import StalePage, fingerprint
    from jev_ultrafast.planning import Check

    class AsyncProgress(FakeBrowser):
        rejected = False

        def act(self, action, page, text=None):
            if not self.rejected:
                self.rejected = True
                # Simulate an external update; the guard rejects before this controller issues input.
                self.page["actions"][0]["value"] = text
                self.page["fingerprint"] = fingerprint(self.page)
                raise StalePage("Field changed before input")
            return super().act(action, page, text)

    objective = "Search Ada Lovelace and open the article"
    data = {"objective": objective, "plan": [
        {"goal": "Enter the query", "texts": [{"label": "Search", "value": "Ada Lovelace"}],
         "checks": [{"kind": "value", "label": "Search", "value": "Ada Lovelace"}]},
        {"goal": "Open the article", "texts": [],
         "checks": [{"kind": "url", "value": "https://example.test/article"}]},
    ]}
    monkeypatch.setattr(loop, "Browser", AsyncProgress)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[chosen("e1"), chosen("e2", "CLICK")]))
    planner = Mock(side_effect=AssertionError("Verified step progress does not need replanning"))
    with ObjectiveAgent("https://example.test/", objective, prepared_plan=data, planner=planner,
                        checks=(Check("url", "https://example.test/article"),)) as agent:
        first = agent.command()
        assert first["status"] == "ready" and first["plan_index"] == 1
        assert first["corrections_used"] == 0 and agent.browser.calls == []
        assert agent.command()["status"] == "done"
        assert agent.browser.calls == [("e2", None)]
    planner.assert_not_called()


@pytest.mark.parametrize("fault", [
    "wrong_function", "multiple_calls", "malformed_arguments", "non_string_arguments", "objective_change",
    "extra_selector", "final_override", "truncated", "content_filtered", "extra_choices",
])
def test_invalid_tool_envelopes_never_fall_back_to_valid_prose(monkeypatch, fault):
    monkeypatch.setenv("PLANNER_API_KEY", "test-only")
    monkeypatch.setenv("PLANNER_MODEL", "large-planner")
    monkeypatch.setenv("PLANNER_BASE_URL", "https://planner.test/v1")
    data = {"objective": "Original task", "plan": []}
    function = {"name": "prepare_plan", "arguments": json.dumps(data)}
    call = {"type": "function", "function": function}
    choice = {"finish_reason": "tool_calls", "message": {"tool_calls": [call], "content": json.dumps(data)}}
    result = {"choices": [choice]}
    if fault == "wrong_function":
        function["name"] = "execute_javascript"
    elif fault == "multiple_calls":
        choice["message"]["tool_calls"].append(call)
    elif fault == "malformed_arguments":
        function["arguments"] = "{"
    elif fault == "non_string_arguments":
        function["arguments"] = data
    elif fault == "objective_change":
        function["arguments"] = json.dumps({"objective": "Different task", "plan": []})
    elif fault == "extra_selector":
        function["arguments"] = json.dumps({**data, "plan": [{
            "goal": "Click", "texts": [], "checks": [{"kind": "text", "value": "Complete"}], "selector": "#button",
        }]})
    elif fault == "final_override":
        function["arguments"] = json.dumps({**data, "final_checks": []})
    elif fault == "extra_choices":
        result["choices"].append(choice)
    else:
        choice["finish_reason"] = "length" if fault == "truncated" else "content_filter"
    post = Mock(return_value=result)
    monkeypatch.setattr(planning, "post_json", post)
    with pytest.raises(ValueError, match="no valid prepared plan"):
        planning.plan_objective({"objective": "Original task"})
    post.assert_called_once()
