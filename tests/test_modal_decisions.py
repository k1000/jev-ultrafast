"""Offline reproduction of a populated picker with pending confirmation, not live Jev reasoning."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import choice, page
from test_objective import FakeBrowser

from jev_ultrafast import Check, ObjectiveAgent, browser, model, planning
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint


@pytest.mark.parametrize("confirmation", ["Apply", "Save changes", "Confirm"])
def test_pending_modal_confirmation_remains_eligible_after_an_unchanged_click(monkeypatch, confirmation):
    class PendingPicker(FakeBrowser):
        def __init__(self, url):
            super().__init__(url)
            self.page.update(modal_open=True, document_id=1, actions=[
                {"id": "edit", "node": 1, "kind": "fill", "role": "textbox",
                 "label": "Deadline", "value": "2030-05-14"},
                {"id": "open", "node": 1, "kind": "click", "role": "textbox",
                 "label": "Open Deadline", "value": "2030-05-14"},
                {"id": "apply", "node": 2, "kind": "click", "role": "button",
                 "label": confirmation, "value": ""},
            ], controls=[
                {"node": 1, "kind": "control", "role": "textbox", "label": "Deadline",
                 "observable": True, "value": "2030-05-14"},
                {"node": 3, "kind": "control", "role": "textbox", "label": "Scheduled date",
                 "observable": False},
            ])
            self.page["fingerprint"] = fingerprint(self.page)

        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))
            if action["id"] == "apply":
                self.page.update(modal_open=False, actions=[], controls=[
                    {"node": 3, "kind": "control", "role": "textbox", "label": "Scheduled date",
                     "observable": True, "readonly": True, "value": "May 14, 2030"},
                ])
                self.page["fingerprint"] = fingerprint(self.page)
            else:
                assert action["id"] == "open"  # Refocusing has no effect; do not replay it.
            return {"status": "executed", "input_started": True}

    requests = []

    def post(_url, _key, body):
        requests.append(deepcopy(body))
        assert body["state"]["page"]["modal_open"] is True
        criteria = body["questions"]["click_target"]["criteria"]
        # An intentionally stubborn stub: prefer refocus whenever offered, else confirm.
        # This tests candidate eligibility, not a learned model's ability to pick confirmation.
        index = next((i for i, c in criteria.items() if "Open Deadline" in c["element"]), None)
        if index is None:
            index = next(i for i, c in criteria.items() if confirmation in c["element"])
        return {"model": "offline-stub", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
            "click_target": choice(criteria, index),
            "type_text_target": {"choice": "invalid-unused-head"},
        }}

    forbidden = Mock(side_effect=AssertionError("Offline test cannot call a browser, planner, or text helper"))
    monkeypatch.setattr(loop, "Browser", PendingPicker)
    monkeypatch.setattr(loop, "choose", model.choose)
    monkeypatch.setattr(loop, "field_texts", forbidden)
    monkeypatch.setattr(planning, "post_json", forbidden)
    monkeypatch.setattr(browser, "cdp", forbidden)
    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    goal = "Apply the deadline of May 14, 2030 to the schedule"
    plan = {"objective": goal, "plan": [{
        "goal": "Set the deadline", "texts": [{"label": "Deadline", "value": "2030-05-14"}],
        "checks": [{"kind": "value", "label": "Scheduled date", "value": "May 14, 2030"}],
    }]}
    with ObjectiveAgent("https://example.test/", goal,
                        checks=(Check("value", "May 14, 2030", label="Scheduled date"),),
                        prepared_plan=plan, planner=forbidden) as controller:
        result = list(controller.run())[-1]
        assert result["status"] == "done" and result["verification"] == [True]
        assert controller.browser.calls == [("open", None), ("apply", None)]
        assert len(result["decisions"]) == len(requests) == 2
        assert all(a["status"] == "executed" for a in result["attempts"])
        assert result["planner_calls"] == result["text_calls"] == []
        assert result["replans_used"] == 0
        assert result["corrections_used"] == 0
        assert all("_last_mutation" not in r["state"]["execution"] for r in requests)
        field = next(e for e in requests[1]["state"]["elements"] if e["role"] == "textbox")
        assert field["value"] == "2030-05-14" and field["operations"] == ["TYPE_TEXT"]
    forbidden.assert_not_called()


@pytest.mark.parametrize("case,excluded", [
    ("same_state", True), ("new_marker", False), ("new_node", False), ("new_value", False),
    ("renumbered_action", True), ("acknowledged_change", False), ("geometry_only", True),
    ("other_operation", False), ("no_guard", False),
])
def test_no_replay_exclusion_is_scoped_to_fresh_operation_node_value_and_marker(monkeypatch, case, excluded):
    p = page()
    p["marker"] = ["document-A", "controls-A"]
    guard = (("click", 10, ""), deepcopy(p["marker"]), deepcopy(p["marker"]))
    if case == "new_marker":
        p["marker"] = ["document-B", "controls-B"]
    elif case == "new_node":
        p["actions"][0]["node"] = p["actions"][1]["node"] = 99
    elif case == "new_value":
        p["actions"][0]["value"] = p["actions"][1]["value"] = "new value"
    elif case == "renumbered_action":
        p["actions"][1]["id"] = "fresh-index"
    elif case == "acknowledged_change":
        guard = (guard[0], ["previous-state"], deepcopy(p["marker"]))
    elif case == "geometry_only":
        p["actions"][1]["rect"] = {"x": 99, "y": 99}
        p["fingerprint"] = fingerprint(p)
    elif case == "other_operation":
        guard = (("select", 10, ""), guard[1], guard[2])
    execution = None if case == "no_guard" else {"objective": "Apply the edit", "_last_mutation": guard}
    original_actions = deepcopy(p["actions"])

    def post(_url, _key, body):
        q = body["questions"]
        assert ("1" not in q["click_target"]["criteria"]) is excluded
        assert set(q["type_text_target"]["criteria"]) == {"1"}
        assert "_last_mutation" not in body["state"].get("execution", {})
        return {"model": "offline-stub", "answers": {
            "operation": choice(q["operation"]["criteria"], "CLICK"),
            "click_target": choice(q["click_target"]["criteria"], "2"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(p, "Apply the edit", [], execution=execution)
    assert result["choice"] == "e3"
    assert p["actions"] == original_actions


def test_live_clock_does_not_restore_an_unchanged_click_candidate(monkeypatch):
    p = page()
    p["marker"] = ["Clock 2"]
    p["semantic_marker"] = ["stable controls", "stable actions"]
    execution = {"_last_mutation": (("click", 10, ""),
                                    ["stable controls", "stable actions"],
                                    ["stable controls", "stable actions"])}

    def post(_url, _key, body):
        criteria = body["questions"]["click_target"]["criteria"]
        assert "1" not in criteria
        return {"model": "offline-stub", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
            "click_target": choice(criteria, "2"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(p, "Complete the edit", [], execution=execution)["choice"] == "e3"


def test_unchanged_select_excludes_only_the_same_option_and_preserves_option_indices(monkeypatch):
    p = page()
    p["marker"] = ["document-A", "stable-control"]
    p["actions"] = [
        {"id": "utc", "kind": "select", "node": 10, "role": "combobox",
         "label": "Timezone → UTC", "value": "UTC", "current_value": "UTC"},
        {"id": "berlin", "kind": "select", "node": 10, "role": "combobox",
         "label": "Timezone → Berlin", "value": "Europe/Berlin", "current_value": "UTC"},
    ]
    execution = {"_last_mutation": (("select", 10, "UTC"), p["marker"], p["marker"])}

    def post(_url, _key, body):
        q = body["questions"]
        assert set(q["select_target"]["criteria"]) == {"1:2"}
        assert body["state"]["elements"][0]["operations"] == ["SELECT"]
        return {"model": "offline-stub", "answers": {
            "operation": choice(q["operation"]["criteria"], "SELECT"),
            "select_target": choice(["1:2"], "1:2"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(p, "Set timezone to Berlin", [], execution=execution)["choice"] == "berlin"


def test_excluding_the_only_click_omits_empty_operation_and_target_heads(monkeypatch):
    p = page()
    p["actions"] = [p["actions"][1]]
    execution = {"_last_mutation": (("click", 10, ""), p["fingerprint"], p["fingerprint"])}

    def post(_url, _key, body):
        assert set(body["questions"]) == {"operation"}
        assert set(body["questions"]["operation"]["criteria"]) == {"DONE", "BLOCKED"}
        assert body["state"]["elements"][0]["operations"] == []
        return {"model": "offline-stub", "answers": {
            "operation": choice(["DONE", "BLOCKED"], "BLOCKED"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    assert model.choose(p, "Complete the edit", [], execution=execution)["choice"] == "BLOCKED"


def test_response_naming_an_excluded_target_is_rejected_not_substituted(monkeypatch):
    p = page()
    execution = {"_last_mutation": (("click", 10, ""), p["fingerprint"], p["fingerprint"])}

    def post(_url, _key, body):
        assert set(body["questions"]["click_target"]["criteria"]) == {"2"}
        return {"model": "offline-stub", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
            "click_target": choice(["1"], "1"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(model.PredictionError) as exc:
        model.choose(p, "Apply the edit", [], execution=execution)
    assert exc.value.diagnostic["stage"] == "selected_target_validation"
