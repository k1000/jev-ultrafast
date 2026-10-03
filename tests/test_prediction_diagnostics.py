"""Prediction diagnostics are safe, offline, and do not relax response validation."""

from unittest.mock import Mock

import pytest

from jev_ultrafast import model


@pytest.fixture
def boundary(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "secret-key")
    post = Mock(side_effect=ValueError("private provider response and secret-key"))
    monkeypatch.setattr(model, "post_json", post)
    return post


def page():
    return {"url": "https://private.example/", "title": "private title", "text": "private text", "actions": [
        {"id": "e1", "node": 1, "kind": "click", "label": "private label", "role": "button"}]}


def test_failed_transport_helper_has_safe_stage_and_elapsed_time(boundary):
    with pytest.raises(model.PredictionError) as caught:
        model.choose(page(), "private objective", [])
    diagnostic = caught.value.diagnostic
    assert diagnostic["stage"] == "transport" and diagnostic["code"] == "request_failed"
    assert diagnostic["transport_attempted"] is True and diagnostic["cause_type"] == "ValueError"
    assert diagnostic["latency_ms"] >= 0
    assert "private" not in str(caught.value) + repr(diagnostic)
    assert "secret-key" not in str(caught.value) + repr(diagnostic)
    boundary.assert_called_once()


@pytest.mark.parametrize("phase", ["observation", "verification", "extra_verification", "execution"])
def test_controller_distinguishes_failure_boundaries_without_messages(monkeypatch, phase):
    from test_objective import FakeBrowser, chosen

    from jev_ultrafast import Check, ObjectiveAgent
    from jev_ultrafast import agent as loop
    from jev_ultrafast import objective as controller_module

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    chooser = Mock(return_value=chosen("e2", "CLICK"))
    monkeypatch.setattr(loop, "choose", chooser)
    with ObjectiveAgent("https://example.test/", "Open an article", checks=(Check("text", "Article"),),
                        prepared_plan={"objective": "Open an article", "plan": []}) as controller:
        failure = Mock(side_effect=ValueError("private exception message and secret-key"))
        if phase == "observation":
            controller.browser.observe = failure
        elif phase == "verification":
            monkeypatch.setattr(controller_module, "evidence", failure)
        elif phase == "extra_verification":
            controller.verifier = failure
        else:
            controller.browser.act = failure
        state = controller.command()
        event = state["events"][-1]
        assert state["status"] == "needs_attention" and state["verification"] is None
        assert event["phase"] == phase and event["error"] == "ValueError"
        assert "diagnostic" not in event and "private" not in repr(event) and "secret" not in repr(event)
        assert len(state["prediction_calls"]) == int(phase == "execution")
        assert len(state["decisions"]) == int(phase == "execution")
        browser = controller.browser
    assert browser.closed is False


def test_settling_records_verifier_failure_not_a_browser_read_failure(monkeypatch):
    from test_objective import chosen
    from test_objective_settle import boundaries

    from jev_ultrafast import Check, ObjectiveAgent

    _, _, _, planner = boundaries(monkeypatch, [chosen("DONE", "DONE")])
    settling = [False]

    def verifier(page):
        if settling[0]:
            raise ValueError("private verifier error")
        return False

    with ObjectiveAgent("https://example.test/", "Open the article", verifier=verifier,
                        checks=(Check("url_contains", "example.test"),), planner=planner,
                        prepared_plan={"objective": "Open the article", "plan": []}) as controller:
        original = controller.browser.settle

        def settle(*args, **kwargs):
            settling[0] = True
            return original(*args, **kwargs)

        controller.browser.settle = settle
        state = controller.command()
        error = controller.browser.settlements[-1]["read_errors"][-1]
        assert error["phase"] == "extra_verification" and error["error"] == "ValueError"
        event = next(e for e in state["events"] if e["kind"] == "outcome_settled")
        assert event["failure_phase"] == "extra_verification"
        assert state["status"] == "needs_attention" and state["verification"] is None
        assert "private" not in repr(event) and controller.browser.calls == []
    planner.assert_not_called()


@pytest.mark.parametrize("field,value,code", [
    ("confidence", True, "invalid_confidence"),
    ("confidence", float("nan"), "invalid_confidence"),
    ("probabilities", [], "invalid_probabilities"),
    ("probabilities", {"a": float("inf"), "b": 0.}, "invalid_probability_values"),
    ("probabilities", {"a": .4, "b": .4}, "probability_mass"),
    ("probabilities", {"a": .2, "b": .8}, "choice_not_maximal"),
])
def test_choice_diagnostics_distinguish_strict_validation_reasons(field, value, code):
    reply = {"choice": "a", "confidence": .9, "probabilities": {"a": 1., "b": 0.}, field: value}
    with pytest.raises(model.PredictionError) as caught:
        model.validate_choice(reply, {"a", "b"}, head="operation")
    assert caught.value.diagnostic == {"stage": "operation_validation", "code": code,
                                       "head": "operation", "expected_choices": 2}


def test_verification_failure_after_safe_stale_rejection_is_not_mislabeled_prediction(monkeypatch):
    from copy import deepcopy

    from test_objective import FakeBrowser, chosen

    from jev_ultrafast import Check, ObjectiveAgent
    from jev_ultrafast import agent as loop
    from jev_ultrafast.browser import StalePage

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    chooser = Mock(return_value=chosen("e2", "CLICK"))
    monkeypatch.setattr(loop, "choose", chooser)
    with ObjectiveAgent("https://example.test/", "Open the article", checks=(Check("text", "Article"),),
                        prepared_plan={"objective": "Open the article", "plan": []}) as controller:
        controller.browser.observe = Mock(side_effect=[deepcopy(controller.browser.page),
                                                       ValueError("private failed read")])
        controller.browser.act = Mock(side_effect=StalePage("Known pre-input rejection"))
        state = controller.command()
        assert state["events"][-1]["phase"] == "observation"
        assert state["history"] == [] and len(state["prediction_calls"]) == len(state["decisions"]) == 1
        assert state["attempts"][-1]["status"] == "rejected_before_input"
    assert chooser.call_count == 1


def test_report_keeps_original_prediction_diagnostics_when_final_evidence_read_fails(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from scripts import objective_flights

    diagnostic = {"stage": "selected_target_validation", "code": "invalid_choice", "head": "click_target",
                  "transport_attempted": True, "latency_ms": 5}
    calls = [{"status": "error", "latency_ms": 5, "diagnostic": diagnostic}]
    events = [{"kind": "execution_error", "phase": "prediction", "diagnostic": diagnostic}]
    browser = Mock(target="owned", cdp_calls=[], observe=Mock(side_effect=RuntimeError("private final read")))
    controller = SimpleNamespace(status="needs_attention", stop_reason="execution_error", events=events,
        browser=browser, agent=SimpleNamespace(state={"prediction_calls": calls, "attempts": []}),
        run=lambda: iter(()))
    monkeypatch.setattr(objective_flights, "FlightsObjective", Mock(return_value=controller))
    report = objective_flights.run(tmp_path / "failure.json")
    assert report["passed"] is False and report["error"] == "RuntimeError"
    assert report["status"] == "needs_attention" and report["retained_tab"] == "owned"
    assert report["jev_prediction_attempts"] == 1 and report["prediction_calls"] == calls
    assert report["events"] == events and "private final read" not in repr(report)
    report["events"][0]["diagnostic"]["stage"] = "tampered"
    assert events[0]["diagnostic"]["stage"] == "selected_target_validation"
    browser.close.assert_not_called()


def answer(ids, selected):
    return {"choice": selected, "confidence": .9,
            "probabilities": {key: 1. if key == selected else 0. for key in ids}}


@pytest.mark.parametrize("case,stage,code,head", [
    ("configuration", "configuration", "missing_configuration", None),
    ("context", "request", "invalid_context", None),
    ("envelope", "envelope", "invalid_answers", None),
    ("operation", "operation_validation", "invalid_choice", "operation"),
    ("target", "selected_target_validation", "probability_keys_mismatch", "click_target"),
    ("metadata", "envelope", "invalid_model", None),
])
def test_failures_identify_only_safe_stages_and_selected_heads(monkeypatch, boundary, case, stage, code, head):
    context = page()
    if case == "configuration":
        monkeypatch.delenv("TYPESAFE_API_KEY")
    elif case == "context":
        context.pop("title")

    def response(_url, _key, body):
        operations = body["questions"]["operation"]["criteria"]
        result = {"model": "test", "answers": {"operation": answer(operations, "CLICK"),
                                               "click_target": answer(["1"], "1")}}
        if case == "envelope":
            return {"answers": "private response"}
        if case == "operation":
            result["answers"]["operation"]["choice"] = "private unsupported operation"
        if case == "target":
            result["answers"]["click_target"]["probabilities"]["private target"] = 0.
        if case == "metadata":
            result.pop("model")
        return result

    boundary.side_effect = response
    with pytest.raises(model.PredictionError) as caught:
        model.choose(context, "private objective", [])
    diagnostic = caught.value.diagnostic
    assert diagnostic["stage"] == stage and diagnostic["code"] == code
    assert diagnostic.get("head") == head
    attempted = case not in {"configuration", "context"}
    assert diagnostic["transport_attempted"] is attempted
    assert boundary.call_count == int(attempted)
    assert "private" not in str(caught.value) + repr(diagnostic)
    assert "secret" not in str(caught.value) + repr(diagnostic)


def test_failed_prediction_is_counted_without_a_successful_decision_or_action(monkeypatch, boundary):
    from test_objective import FakeBrowser

    from jev_ultrafast import Check, ObjectiveAgent
    from jev_ultrafast import agent as loop

    monkeypatch.setattr(loop, "Browser", FakeBrowser)
    with ObjectiveAgent("https://example.test/", "Open an article", checks=(Check("text", "Article"),),
                        prepared_plan={"objective": "Open an article", "plan": []}) as controller:
        state = controller.command()
        assert state["status"] == "needs_attention"
        assert state["decisions"] == [] and state["history"] == [] and controller.browser.calls == []
        assert len(state["prediction_calls"]) == 1
        call = state["prediction_calls"][0]
        assert call["status"] == "error" and call["latency_ms"] >= 0
        assert call["diagnostic"]["stage"] == "transport"
        event = state["events"][-1]
        assert event["phase"] == "prediction" and event["diagnostic"] == call["diagnostic"]
        assert "private" not in repr(call) + repr(event) and "secret" not in repr(call) + repr(event)
        browser = controller.browser
        state["prediction_calls"][0]["diagnostic"]["stage"] = "tampered"
        state["events"][-1]["diagnostic"]["stage"] = "tampered"
        assert controller.snapshot()["prediction_calls"][0]["diagnostic"]["stage"] == "transport"
        assert controller.snapshot()["events"][-1]["diagnostic"]["stage"] == "transport"
    assert not browser.closed
    boundary.assert_called_once()
