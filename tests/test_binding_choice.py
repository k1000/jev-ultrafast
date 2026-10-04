"""Finite prepared-value heads are speculative, selected-only and code validated offline."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_agent import choice, page
from test_late_bindings import GOAL, PLAN, VALUE, ChangingView, install
from test_objective import chosen

from jev_ultrafast import Check, ObjectiveAgent, model
from jev_ultrafast import agent as loop
from jev_ultrafast.browser import fingerprint


def test_binding_heads_are_per_target_in_one_request_only_selected_head_validated(monkeypatch):
    state = page()
    state["actions"] = [
        {"id": "first", "node": 10, "kind": "fill", "role": "textbox", "label": "First", "value": ""},
        {"id": "other", "node": 11, "kind": "fill", "role": "textbox", "label": "Other", "value": ""},
    ]
    execution = {"late_bindings": True, "_binding_choices": {
        "first": {"s1t0": {"value": "TRK-42", "label": "Old name"},
                  "s1t1": {"value": "DROP-7", "label": "Other old name"}},
        "other": {"s1t0": {"value": "TRK-42", "label": "Old name"}},
    }}
    requests = []

    def post(_url, _key, body):
        requests.append(body)
        questions = body["questions"]
        head = questions["binding_target_1"]["criteria"]
        assert set(head) == {"NONE", "s1t0", "s1t1"}
        assert set(questions["binding_target_2"]["criteria"]) == {"NONE", "s1t0"}
        assert "_binding_choices" not in body["state"]["execution"]
        return {"model": "offline", "answers": {
            "operation": choice(questions["operation"]["criteria"], "TYPE_TEXT"),
            "type_text_target": choice(questions["type_text_target"]["criteria"], "1"),
            "binding_target_1": choice(head, "s1t1"),
            "binding_target_2": {"choice": "malformed unused head"},
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(state, "Enter a supplied value", [], execution=execution)
    assert result["choice"] == "first" and result["binding_choice"] == "s1t1"
    assert len(requests) == 1


@pytest.mark.parametrize("bad", [None, "NONE", "s99t0", 7])
def test_controller_rejects_missing_none_invalid_selected_binding_even_with_chooser_stub(monkeypatch, bad):
    browser = ChangingView("https://example.test/")
    monkeypatch.setattr(loop, "Browser", lambda url: browser)

    def decide(_page, _goal, history, *, execution):
        return chosen("e2", "CLICK") if not history else chosen("e3") | (
            {} if bad is None else {"binding_choice": bad})

    monkeypatch.setattr(loop, "choose", decide)
    helper = Mock(side_effect=AssertionError("No text generation"))
    monkeypatch.setattr(loop, "field_texts", helper)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN),
                        late_bindings=True, checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        last = list(agent.run())[-1]
        assert last["status"] != "done" and browser.calls == [("e2", None)]
        assert last["text_calls"] == []
    helper.assert_not_called()


def test_selected_binding_head_invalid_id_fails_before_controller_input(monkeypatch):
    state = page()
    state["actions"] = [{"id": "only", "node": 10, "kind": "fill", "role": "textbox",
                         "label": "Recipient", "value": ""}]
    execution = {"_binding_choices": {"only": {"s0t0": {"value": "TRK-42", "label": "Old name"}}}}

    def post(_url, _key, body):
        q = body["questions"]
        return {"model": "offline", "answers": {
            "operation": choice(q["operation"]["criteria"], "TYPE_TEXT"),
            "type_text_target": choice(q["type_text_target"]["criteria"], "1"),
            "binding_target_1": {"choice": "invented", "probabilities": {"NONE": .5, "s0t0": .5},
                                 "confidence": .5},
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(model.PredictionError) as caught:
        model.choose(state, "Enter supplied reference", [], execution=execution)
    assert caught.value.diagnostic["head"] == "binding_target_1"
    assert caught.value.diagnostic["stage"] == "selected_target_validation"


def test_two_current_source_slots_offer_finite_choices_but_none_types_nothing(monkeypatch):
    browser = ChangingView("https://example.test/")
    monkeypatch.setattr(loop, "Browser", lambda url: browser)
    offered = []

    def decide(_page, _goal, history, *, execution):
        if not history:
            return chosen("e2", "CLICK")
        offered.append(deepcopy(execution.get("_binding_choices", {}).get("e3", {})))
        return chosen("e3") | {"binding_choice": "NONE"}

    monkeypatch.setattr(loop, "choose", decide)
    plan = deepcopy(PLAN)
    plan["plan"][1]["texts"].append({"label": "Former delivery", "value": "DROP-7"})
    plan["plan"][1]["checks"].append({"kind": "value", "label": "Former delivery", "value": "DROP-7"})
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=plan, late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        result = list(agent.run())[-1]
        assert result["status"] != "done" and browser.calls == [("e2", None)]
        assert set(offered[0]) == {"s1t0", "s1t1"}
        assert agent.value_owners == {}


def test_overlapping_exact_prepared_bindings_never_choose_first_silently(monkeypatch):
    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    plan = deepcopy(PLAN)
    plan["plan"][1]["texts"] = [
        {"label": "Tracking reference", "value": VALUE},
        {"label": "Tracking reference", "role": "textbox", "value": "DIFFERENT"}]
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=plan, late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        result = list(agent.run())[-1]
        assert result["status"] != "done" and browser.calls == [("e2", None)]
        assert agent.value_owners == {}


def test_unreadable_observable_value_does_not_release_acknowledged_source_slot(monkeypatch):
    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        assert list(agent.run())[-1]["status"] == "done"
        assert (1, 0) in agent.value_owners
        browser.page["controls"][1].pop("value")
        agent.release_owners(browser.page)
        assert (1, 0) in agent.value_owners
        browser.page["controls"][1]["value"] = VALUE
        browser.page["controls"].append({**browser.page["controls"][1], "value": "ambiguous fact"})
        agent.release_owners(browser.page)
        assert (1, 0) in agent.value_owners
        browser.page["controls"].pop()
        browser.page["controls"][1]["value"] = "contradictory readable value"
        agent.release_owners(browser.page)
        assert (1, 0) not in agent.value_owners


@pytest.mark.parametrize("unknown", [None, float("nan"), float("inf"), False, ""])
def test_unknown_document_retains_owner_and_cannot_rebind_until_known_contradiction(monkeypatch, unknown):
    from jev_ultrafast.objective import PreparedTextUnavailable

    browser = ChangingView("https://example.test/")
    install(monkeypatch, browser)
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        assert list(agent.run())[-1]["status"] == "done"
        original = browser.page["document_id"]
        assert (1, 0) in agent.value_owners
        if unknown is None:
            browser.page.pop("document_id")
        else:
            browser.page["document_id"] = unknown
        agent.release_owners(browser.page)
        assert (1, 0) in agent.value_owners
        browser.page["controls"][1]["value"] = "untrusted contradiction without document identity"
        agent.release_owners(browser.page)
        assert (1, 0) in agent.value_owners
        browser.page["controls"][1]["value"] = VALUE
        original_binding = {**browser.page["actions"][0], "id": "returned", "node": 99,
                            "label": "Search", "value": ""}
        browser.page["actions"].append(original_binding)
        browser.page["controls"].append(original_binding)
        assert agent.binding_candidates(browser.page) == {}
        with pytest.raises(PreparedTextUnavailable):
            agent.prepared_value(original_binding, browser.page)
        assert browser.calls == [("e2", None), ("e3", VALUE)]
        browser.page["document_id"] = original
        agent.release_owners(browser.page)
        assert (1, 0) in agent.value_owners
        with pytest.raises(PreparedTextUnavailable):
            agent.prepared_value(original_binding, browser.page)
        browser.page["document_id"] = "different-known-document"
        agent.release_owners(browser.page)
        assert (1, 0) not in agent.value_owners


def test_more_than_eight_fill_targets_fail_closed_without_silent_head_trimming(monkeypatch):
    browser = ChangingView("https://example.test/")
    monkeypatch.setattr(loop, "Browser", lambda url: browser)
    choices = []

    def decide(page, _goal, history, *, execution):
        if not history:
            return chosen("e2", "CLICK")
        choices.append(execution.get("_binding_choices", {}))
        return chosen("e3") | {"binding_choice": "s1t0"}

    monkeypatch.setattr(loop, "choose", decide)
    original_act = browser.act

    def add_targets(action, page, text=None):
        original_act(action, page, text)
        if action["kind"] == "click":
            extras = [{**browser.page["actions"][0], "id": f"extra-{i}", "node": i + 10,
                       "label": f"Other {i}"} for i in range(8)]
            browser.page["actions"].extend(extras)
            browser.page["controls"].extend(extras)
            browser.page["fingerprint"] = fingerprint(browser.page)

    browser.act = add_targets
    with ObjectiveAgent(browser.page["url"], GOAL, prepared_plan=deepcopy(PLAN), late_bindings=True,
                        checks=(Check("value", VALUE, label="Tracking reference"),)) as agent:
        result = list(agent.run())[-1]
        assert result["status"] != "done" and browser.calls == [("e2", None)]
        assert choices and all(not options for options in choices)
