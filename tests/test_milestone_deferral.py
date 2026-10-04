"""BLOCKED is advisory before the last milestone, never a final-outcome proof."""

from copy import deepcopy
from unittest.mock import Mock

from test_objective import FakeBrowser, chosen

from jev_ultrafast import Check, ObjectiveAgent
from jev_ultrafast import agent as loop


def test_intermediate_blocked_defers_only_its_step_then_later_step_verifies_not_final_goal(monkeypatch):
    class UnsupportedPage(FakeBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page["controls"] = [
                {"node": 70 + i, "kind": "control", "role": "slider", "label": "Price cap", "group": "Filters",
                 "value": "secret", "observable": True, "disabled": False, "readonly": False}
                for i in range(20)
            ] + [
                {"node": 999, "kind": "control", "role": "textbox", "label": "Hidden", "observable": False},
                {"node": 1, "kind": "control", "role": "textbox", "label": "Search", "observable": True},
            ]

    captures = []

    def choose(_page, _goal, history, *, execution):
        captures.append(deepcopy(execution))
        if execution["step_index"] == 0:
            return chosen("BLOCKED", "BLOCKED")
        if not history:
            return chosen("e2", "CLICK")
        return chosen("BLOCKED", "BLOCKED")

    forbidden = Mock(side_effect=AssertionError("No model, planner or text helper transport"))
    monkeypatch.setattr(loop, "Browser", UnsupportedPage)
    monkeypatch.setattr(loop, "choose", choose)
    monkeypatch.setattr(loop, "field_texts", forbidden)
    goal = "Open the article and find its unavailable supplement"
    plan = {"objective": goal, "plan": [
        {"goal": "Apply a price filter", "texts": [],
         "checks": [{"kind": "value", "label": "Price cap", "value": "100"}]},
        {"goal": "Open the article", "texts": [],
         "checks": [{"kind": "url", "value": "https://example.test/article"}]},
    ]}
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/article/supplement"),),
                        prepared_plan=plan, planner=forbidden, local_attempts=0) as controller:
        first = controller.command()
        assert first["status"] == "ready" and first["deferred_steps"] == [0], first["events"]
        assert first["verified_steps"] == [] and first["verification"] == [False]
        assert next(e for e in first["events"] if e["kind"] == "milestone_deferred")["reason"] == "model_blocked"
        assert controller.browser.calls == [] and first["corrections_used"] == 0
        unsupported = captures[0]["unsupported_controls"]
        assert len(unsupported) == 16 and unsupported[0] == {
            "role": "slider", "label": "Price cap", "group": "Filters", "disabled": False, "readonly": False}
        assert "secret" not in str(unsupported) and "node" not in str(unsupported)
        assert "Hidden" not in str(unsupported) and "Search" not in str(unsupported)
        second = controller.command()
        assert second["status"] == "ready" and second["verified_steps"] == [1]
        assert second["deferred_steps"] == [0] and second["verification"] == [False]
        assert controller.browser.calls == [("e2", None)]
        final = controller.command()
        assert final["status"] == "abandoned" and final["stop_reason"] == "replanning_exhausted"
        assert final["verification"] == [False] and final["deferred_steps"] == [0]
        assert final["verified_steps"] == [1]
        assert captures[1]["step_index"] == 1 and captures[2]["step_index"] == 2
    forbidden.assert_not_called()


def test_last_milestone_blocked_remains_bounded_recovery_not_deferred(monkeypatch):
    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    decisions = Mock(return_value=chosen("BLOCKED", "BLOCKED"))
    monkeypatch.setattr(loop, "choose", decisions)
    goal = "Open unavailable article"
    plan = {"objective": goal, "plan": [
        {"goal": "Open the article", "texts": [],
         "checks": [{"kind": "url", "value": "https://example.test/missing"}]},
    ]}
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("url", "https://example.test/missing"),),
                        prepared_plan=plan, max_replans=0) as controller:
        last = list(controller.run())[-1]
        assert last["status"] == "abandoned" and last["stop_reason"] == "replanning_exhausted"
        assert last["deferred_steps"] == [] and last["verified_steps"] == []
        assert last["verification"] == [False] and len(decisions.call_args_list) == 3
        assert controller.browser.calls == []
