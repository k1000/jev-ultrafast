"""Prepared-plan execution contracts. All model and browser boundaries are offline."""

from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import planning
from jev_ultrafast.browser import StalePage, fingerprint
from jev_ultrafast.planning import Check, parse_plan, verify


def test_prepared_plan_is_data_and_cannot_replace_caller_success_checks():
    plan = parse_plan({"objective": "Enter Ada Lovelace in Search", "plan": [{
        "goal": "Enter the requested search query",
        "texts": [{"label": "Search", "value": "Ada Lovelace"}],
        "checks": [{"kind": "value", "label": "Search", "value": "Ada Lovelace"}],
    }]}, objective="Enter Ada Lovelace in Search")
    assert plan.steps[0].texts[0].value == "Ada Lovelace"
    checks = (Check("url", "https://example.test/article"),)
    assert verify({"url": "https://example.test/", "actions": []}, checks) == [False]
    with pytest.raises(ValueError):
        parse_plan({"objective": "Enter Ada Lovelace in Search", "final_checks": [
            {"kind": "text", "value": "anything"}]})
    with pytest.raises(ValueError):
        parse_plan({"objective": "Enter Ada Lovelace in Search", "plan": [{"goal": "Click", "texts": [],
                           "checks": [], "selector": "#button"}]})


def test_native_select_value_uses_dom_value_not_display_label_or_an_offered_option():
    page = {"actions": [
        {"node": 1, "kind": "select", "label": "Category → One", "value": "1",
         "current_value": "Three", "control_value": "3"},
        {"node": 1, "kind": "select", "label": "Category → Two", "value": "2",
         "current_value": "Three", "control_value": "3"},
    ]}
    assert verify(page, (Check("value", "3", label="Category"),)) == [True]
    assert verify(page, (Check("value", "Three", label="Category"),)) == [False]
    assert verify(page, (Check("value", "2", label="Category"),)) == [False]
    page["actions"] = [{"node": 1, "kind": "scroll_to", "label": "Reveal Category",
                        "value": "Three", "control_value": "3"}]
    assert verify(page, (Check("value", "3", label="Category"),)) == [True]


class FakeBrowser:
    def __init__(self, url):
        self.page = {"url": url, "title": "Search", "text": "Search", "scroll": {"y": 0}, "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 1},
            {"id": "e2", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 2},
        ]}
        self.page["fingerprint"] = fingerprint(self.page)
        self.calls = []
        self.closed = False

    def observe(self, screenshot=False):
        return deepcopy(self.page)

    def settle(self, checks, *, timeout=5, interval=.1, verifier=None):
        # This static boundary has no async UI; dedicated tests use the real polling implementation.
        page = self.observe(screenshot=False)
        base = all(verify(page, checks))
        extra = verifier(page) if verifier is not None else True
        return {"status": "verified" if base and extra else "timed_out", "reads": 1,
                "elapsed_ms": 0, "read_errors": []}

    def fresh(self, page, action=None, *, terminal=False):
        return page["fingerprint"] == self.page["fingerprint"]

    def act(self, action, page, text=None):
        self.calls.append((action["id"], text))
        if action["kind"] == "fill":
            self.page["actions"][0]["value"] = text
        else:
            self.page.update(url="https://example.test/article", title="Article", text="Article")
        self.page["fingerprint"] = fingerprint(self.page)

    def close(self):
        self.closed = True


def chosen(selected, operation="TYPE_TEXT"):
    return {"choice": selected, "operation": operation, "target": "1", "confidence": 1.,
            "probabilities": {selected: 1.}, "latency_ms": 1, "usage": {}}


def search_plan(value="Ada Lovelace", objective="Enter Ada Lovelace in Search"):
    return {"objective": objective, "plan": [{"goal": "Enter the query", "texts": [{"label": "Search", "value": value}],
                       "checks": [{"kind": "value", "label": "Search", "value": value}]}]}


def test_jev_executes_prepared_text_without_a_per_action_llm(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    helper = Mock(side_effect=AssertionError("No per-action LLM allowed"))
    monkeypatch.setattr(loop, "field_texts", helper)
    planner = Mock(side_effect=AssertionError("Prepared plan should not need planning"))
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner) as agent:
        result = agent.command("tick")
        assert result["status"] == "done" and result["stop_reason"] == "verified"
        assert result["verification"] == [True]
        assert agent.browser.calls == [("e1", "Ada Lovelace")]
        assert result["planner_calls"] == []
    helper.assert_not_called()
    planner.assert_not_called()


def test_one_initial_plan_then_jev_advances_verified_milestones(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    objective = "Search Ada Lovelace and open the article"
    plan = {"objective": objective, "plan": [
        {"goal": "Enter the query", "texts": [{"label": "Search", "value": "Ada Lovelace"}],
         "checks": [{"kind": "value", "label": "Search", "value": "Ada Lovelace"}]},
        {"goal": "Open the article", "texts": [],
         "checks": [{"kind": "url", "value": "https://example.test/article"}]},
    ]}
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    choices = Mock(side_effect=[chosen("e1"), chosen("e2", "CLICK")])
    monkeypatch.setattr(loop, "choose", choices)
    planner = Mock(return_value=(plan, {"model": "large-model", "latency_ms": 10}))
    with ObjectiveAgent("https://example.test/", objective,
                        checks=(Check("url", "https://example.test/article"),), planner=planner) as agent:
        states = list(agent.run())
        assert states[-1]["status"] == "done"
        assert len(states[-1]["planner_calls"]) == 1
        assert states[-1]["text_calls"] == []
        assert any(e["kind"] == "milestone_verified" for e in states[-1]["events"])
    planner.assert_called_once()
    assert [call.args[1] for call in choices.call_args_list] == ["Enter the query", "Open the article"]
    assert all(call.kwargs["execution"]["objective"] == objective for call in choices.call_args_list)
    assert choices.call_args_list[1].args[0]["actions"][0]["value"] == "Ada Lovelace"


def test_big_model_is_called_only_after_jev_local_corrections_fail(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(side_effect=[
        chosen("BLOCKED", "BLOCKED"), chosen("BLOCKED", "BLOCKED"), chosen("BLOCKED", "BLOCKED"), chosen("e1"),
    ]))
    unresolved = search_plan()
    unresolved["plan"][0]["texts"] = []
    planner = Mock(return_value=(search_plan(), {"model": "large-model"}))
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=unresolved, planner=planner) as agent:
        for _ in range(2):
            assert agent.command()["status"] == "ready"
            planner.assert_not_called()
        result = agent.command()
        planner.assert_called_once()
        assert result["planner_calls"][0]["reason"] == "model_blocked"
        assert agent.browser.calls == []
        result = agent.command()
        assert result["status"] == "done" and result["verification"] == [True]
        assert agent.browser.calls == [("e1", "Ada Lovelace")]
        assert result["goal"] == "Enter Ada Lovelace in Search"


def test_false_done_does_not_satisfy_caller_verifier_and_replanning_is_bounded(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("DONE", "DONE")))
    objective = "Open the missing article"
    planner = Mock(return_value=({"objective": objective, "plan": []}, {"model": "large-model"}))
    with ObjectiveAgent("https://example.test/", objective,
                        checks=(Check("url", "https://example.test/missing"),),
                        prepared_plan={"objective": objective, "plan": []}, planner=planner) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "abandoned" and result["stop_reason"] == "replanning_exhausted"
        assert result["verification"] == [False]
        assert len(result["planner_calls"]) == 1
        assert agent.browser.calls == []
        assert "configured recovery limits" in result["events"][-1]["scope"]
    planner.assert_called_once()


def test_execution_uncertainty_stops_without_replanning_or_repeating(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    class UncertainBrowser(FakeBrowser):
        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))
            raise RuntimeError("CDP reply interrupted after possible input")

    monkeypatch.setattr(loop, "Browser", UncertainBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "needs_attention" and result["stop_reason"] == "execution_error"
        assert agent.browser.calls == [("e1", "Ada Lovelace")]
    planner.assert_not_called()


@pytest.mark.parametrize("error", [RuntimeError, StalePage])
def test_post_mutation_observation_failure_retains_execution_log_and_marks_verification_unavailable(monkeypatch, error):
    from jev_ultrafast import ObjectiveAgent

    class ObservationFailure(FakeBrowser):
        def observe(self, screenshot=False):
            if self.calls:
                raise error("Observation unavailable after input")
            return super().observe(screenshot)

    monkeypatch.setattr(loop, "Browser", ObservationFailure)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner) as agent:
        result = agent.command()
        assert result["status"] == "needs_attention" and result["verification"] is None
        assert len(result["history"]) == 1 and result["history"][0]["text_source"] == "prepared"
        assert agent.browser.calls == [("e1", "Ada Lovelace")]
    planner.assert_not_called()


def test_pre_input_stale_guard_refreshes_without_repeating_a_mutation(monkeypatch):
    from jev_ultrafast import ObjectiveAgent

    class StaleGuard(FakeBrowser):
        rejected = False

        def act(self, action, page, text=None):
            if not self.rejected:
                self.rejected = True
                raise StalePage("Guard rejected before input")
            return super().act(action, page, text)

    monkeypatch.setattr(loop, "Browser", StaleGuard)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner) as agent:
        result = agent.command()
        assert result["status"] == "ready" and result["stale_retries"] == 1
        assert agent.browser.calls == []
        assert agent.command()["status"] == "done"
        assert agent.browser.calls == [("e1", "Ada Lovelace")]
    planner.assert_not_called()


def test_interrupted_mutation_cannot_be_resumed_or_replanned(monkeypatch):
    from jev_ultrafast import ObjectiveAgent

    class Interrupted(FakeBrowser):
        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))
            raise KeyboardInterrupt()

    monkeypatch.setattr(loop, "Browser", Interrupted)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner) as agent:
        with pytest.raises(KeyboardInterrupt):
            agent.command()
        assert agent.snapshot()["verification"] is None
        assert agent.status == "needs_attention" and agent.stop_reason == "interrupted"
        with pytest.raises(ValueError):
            agent.command()
        assert agent.browser.calls == [("e1", "Ada Lovelace")]
    planner.assert_not_called()


def test_known_document_loading_waits_without_planning_or_mutating(monkeypatch):
    from jev_ultrafast import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    choices = Mock(return_value=chosen("e1"))
    monkeypatch.setattr(loop, "choose", choices)
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner, local_attempts=0) as agent:
        agent.browser.page["ready_state"] = "interactive"
        result = agent.command()
        assert result["status"] == "ready" and agent.browser.calls == []
        assert result["events"][-1]["kind"] == "awaiting_load"
        choices.assert_not_called()
        planner.assert_not_called()
        agent.browser.page["ready_state"] = "complete"
        assert agent.command()["status"] == "done"
    planner.assert_not_called()


def test_already_verified_goal_needs_neither_planner_nor_jev(monkeypatch):
    from jev_ultrafast import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    choices, planner = Mock(), Mock()
    monkeypatch.setattr(loop, "choose", choices)
    with ObjectiveAgent("https://example.test/", "Open example.test",
                        checks=(Check("url", "https://example.test/"),), planner=planner) as agent:
        result = agent.command()
        assert result["status"] == "done" and result["planner_calls"] == []
        assert agent.browser.calls == []
    choices.assert_not_called()
    planner.assert_not_called()


def test_planning_client_uses_forced_function_call_and_validates_data(monkeypatch):
    import json

    objective = "Enter Ada Lovelace"
    expected = search_plan(objective=objective)
    monkeypatch.setenv("PLANNER_API_KEY", "test-only")
    monkeypatch.setenv("PLANNER_BASE_URL", "https://planner.test/v1")
    monkeypatch.setenv("PLANNER_MODEL", "large-planning-model")
    post = Mock(return_value={"choices": [{"finish_reason": "tool_calls", "message": {
        "tool_calls": [{"type": "function", "function": {
            "name": "prepare_plan", "arguments": json.dumps(expected),
        }}],
    }}], "usage": {"prompt_tokens": 10}})
    monkeypatch.setattr(planning, "post_json", post)
    context = {"objective": objective, "final_checks": [{"kind": "value", "value": "Ada Lovelace"}]}
    data, meta = planning.plan_objective(context)
    assert data == expected and meta["model"] == "large-planning-model"
    assert post.call_args.args[0] == "https://planner.test/v1/chat/completions"
    body = post.call_args.args[2]
    assert body["model"] == "large-planning-model"
    assert body["tool_choice"] == {"type": "function", "function": {"name": "prepare_plan"}}
    assert "response_format" not in body
    schema = body["tools"][0]["function"]["parameters"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["plan"]["type"] == "array"
    assert schema["properties"]["objective"]["enum"] == [objective]
    assert json.loads(body["messages"][1]["content"]) == context
    post.return_value = {"choices": [{"message": {"content": json.dumps(expected)}}]}
    with pytest.raises(ValueError, match="no valid prepared plan"):
        planning.plan_objective(context)


def test_time_limit_stops_before_planning(monkeypatch):
    from jev_ultrafast import objective

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    clock = [0.]
    monkeypatch.setattr(objective.time, "perf_counter", lambda: clock[0])
    planner = Mock()
    with objective.ObjectiveAgent("https://example.test/", "Open missing article",
                                  checks=(Check("url", "https://example.test/missing"),),
                                  planner=planner, max_seconds=1) as agent:
        clock[0] = 2.
        result = agent.command()
        assert result["status"] == "abandoned" and result["stop_reason"] == "time_budget"
        assert agent.browser.calls == []
    planner.assert_not_called()


def test_deadline_expiring_during_jev_prediction_prevents_input(monkeypatch):
    from jev_ultrafast import objective

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    clock = [100.]
    monkeypatch.setattr(objective.time, "perf_counter", lambda: clock[0])

    def slow_choice(*args, **kwargs):
        clock[0] = 102.
        return chosen("e1")

    monkeypatch.setattr(loop, "choose", slow_choice)
    with objective.ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                                  checks=(Check("value", "Ada Lovelace", label="Search"),),
                                  prepared_plan=search_plan(), max_seconds=1) as agent:
        result = agent.command()
        assert result["status"] == "abandoned" and result["stop_reason"] == "time_budget"
        assert agent.browser.calls == []


def test_unchanged_mutation_is_not_blindly_replayed_for_unmet_checks(monkeypatch):
    from jev_ultrafast import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e2", "CLICK")))
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Find Missing content",
                        checks=(Check("text", "Missing content"),),
                        prepared_plan={"objective": "Find Missing content", "plan": []},
                        planner=planner, local_attempts=0, max_replans=0) as agent:
        assert agent.command()["status"] == "ready"
        assert agent.command()["status"] == "ready"
        result = agent.command()
        assert result["status"] == "abandoned" and result["verification"] == [False]
        assert result["events"][-1]["reason"] == "repeated_mutation"
        assert len(agent.browser.calls) == 2
    planner.assert_not_called()


def test_missing_text_escalates_before_any_typing_then_uses_revised_plan(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    helper = Mock(side_effect=AssertionError("No text helper fallback"))
    monkeypatch.setattr(loop, "field_texts", helper)
    planner = Mock(return_value=(search_plan(), {"model": "large-model"}))
    missing = search_plan()
    missing["plan"][0]["texts"] = []
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=missing, planner=planner) as agent:
        for _ in range(2):
            assert agent.command()["status"] == "ready"
            assert agent.browser.calls == []
            planner.assert_not_called()
        result = agent.command()
        assert result["planner_calls"][0]["reason"] == "missing_prepared_text"
        assert agent.browser.calls == []
        assert agent.command()["status"] == "done"
        assert agent.browser.calls == [("e1", "Ada Lovelace")]
    helper.assert_not_called()


def test_ambiguous_labels_abandon_without_guessing_or_typing(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    class DuplicateBrowser(FakeBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page["actions"].append({**self.page["actions"][0], "id": "e3", "node": 3})
            self.page["fingerprint"] = fingerprint(self.page)

    monkeypatch.setattr(loop, "Browser", DuplicateBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e1")))
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner, local_attempts=0, max_replans=0) as agent:
        result = agent.command()
        assert result["status"] == "abandoned" and result["verification"] == [False]
        assert result["events"][-1]["reason"] == "missing_prepared_text"
        assert agent.browser.calls == []
    planner.assert_not_called()


def test_planner_cannot_weaken_final_criteria(monkeypatch):
    from jev_ultrafast.objective import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    objective = "Open missing article"
    planner = Mock(return_value=({"objective": objective, "final_checks": [{"kind": "text", "value": "Search"}],
                                  "plan": []}, {}))
    with ObjectiveAgent("https://example.test/", objective,
                        checks=(Check("url", "https://example.test/missing"),), planner=planner) as agent:
        result = agent.command()
        assert result["status"] == "needs_attention" and result["stop_reason"] == "planning_failed"
        assert result["verification"] == [False] and agent.browser.calls == []


@pytest.mark.parametrize("limit, reason", [("max_decisions", "decision_budget"), ("max_actions", "action_budget")])
def test_global_limits_survive_correction_cycles(monkeypatch, limit, reason):
    from jev_ultrafast.objective import ObjectiveAgent

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value=chosen("e2", "CLICK")))
    planner = Mock()
    with ObjectiveAgent("https://example.test/", "Find Missing content",
                        checks=(Check("text", "Missing content"),),
                        prepared_plan={"objective": "Find Missing content", "plan": []},
                        planner=planner, **{limit: 1}) as agent:
        result = list(agent.run())[-1]
        assert result["status"] == "abandoned" and result["stop_reason"] == reason
        assert len(agent.browser.calls) == 1
    planner.assert_not_called()
