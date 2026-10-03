"""Offline contracts for a dynamic operation/target policy. No paid APIs."""

import json
import time
from copy import deepcopy
from threading import BoundedSemaphore
from unittest.mock import Mock

import pytest

from jev_ultrafast import agent as loop
from jev_ultrafast import model
from jev_ultrafast.browser import StalePage, browser_operation, fingerprint


def page():
    state = {
        "url": "https://example.test/",
        "title": "Search",
        "text": "Search",
        "scroll": {"y": 0},
        "actions": [
            {"id": "e1", "kind": "fill", "label": "Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e2", "kind": "click", "label": "Open Search", "role": "textbox", "value": "", "node": 10},
            {"id": "e3", "kind": "click", "label": "Go", "role": "button", "value": "", "node": 20},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
    }
    state["fingerprint"] = fingerprint(state)
    return state


def choice(ids, selected):
    return {"choice": selected, "confidence": 1.0, "probabilities": {i: float(i == selected) for i in ids}}


def decision(action="e1"):
    return {
        "choice": action,
        "operation": "TYPE_TEXT",
        "target": "1",
        "confidence": 1.0,
        "probabilities": {action: 1.0},
        "latency_ms": 10,
        "usage": {},
    }


@pytest.mark.parametrize("mutation", ["unknown", "nan", "missing", "negative", "non_max", "confidence"])
def test_invalid_choice_is_rejected(mutation):
    a = choice(["a", "b"], "a")
    if mutation == "unknown":
        a["choice"] = "invented"
    elif mutation == "nan":
        a["probabilities"]["a"] = float("nan")
    elif mutation == "missing":
        del a["probabilities"]["b"]
    elif mutation == "negative":
        a["probabilities"]["b"] = -1
    elif mutation == "non_max":
        a["choice"] = "b"
    else:
        a["confidence"] = 5
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.validate_choice(a, {"a", "b"})


def test_one_index_per_node_with_operation_specific_targets():
    elements, targets, controls = model.action_space(page()["actions"])
    assert len(elements) == 2
    assert elements[0]["operations"] == ["TYPE_TEXT", "CLICK"]
    assert targets["TYPE_TEXT"]["1"]["id"] == "e1"
    assert targets["CLICK"]["1"]["id"] == "e2"
    assert targets["CLICK"]["2"]["id"] == "e3"
    assert "WAIT" in controls


def test_offscreen_target_has_its_own_operation_and_group(monkeypatch):
    p = page()
    p["actions"].append({
        "id": "e4", "kind": "scroll_to", "label": "Reveal Context Language Models", "node": 44,
        "role": "link", "group": "Research", "position": "offscreen",
    })
    elements, targets, _ = model.action_space(p["actions"])
    assert targets["SCROLL_TO"]["3"]["id"] == "e4"
    assert elements[2]["group"] == "Research"

    def post(_url, _key, body):
        options = body["questions"]["scroll_to_target"]["criteria"]
        assert options["3"]["group"] == "Research"
        return {"model": "test", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "SCROLL_TO"),
            "scroll_to_target": choice(options, "3"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(p, "Open Context Language Models", [])
    assert result["operation"] == "SCROLL_TO" and result["choice"] == "e4"


def test_all_heads_are_one_request_and_only_matching_head_executes(monkeypatch):
    calls = []

    def post(_url, _key, body):
        calls.append(body)
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "TYPE_TEXT"),
                "type_text_target": choice(["1"], "1"),
                "click_target": {"choice": "invented"},
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(page(), "Find a book", [])
    assert len(calls) == 1
    assert d["operation"] == "TYPE_TEXT" and d["target"] == "1" and d["choice"] == "e1"
    assert set(calls[0]["questions"]) == {"operation", "click_target", "type_text_target"}


def test_click_cannot_consume_a_text_target(monkeypatch):
    def post(_url, _key, body):
        return {
            "model": "test",
            "answers": {
                "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
                "type_text_target": choice(["1"], "1"),
                "click_target": choice(["1", "2", "999"], "999"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    with pytest.raises(ValueError, match="Invalid TypeSafe"):
        model.choose(page(), "Find a book", [])


def test_target_head_receives_control_state_and_full_next_step_rules(monkeypatch):
    p = page()
    p["actions"].insert(0, {
        "id": "toggle", "kind": "click", "label": "Free cancellation", "node": 30,
        "role": "checkbox", "checked": "true", "selected": False,
    })

    def post(_url, _key, body):
        questions = body["questions"]
        target = questions["click_target"]
        assert target["criteria"]["1"]["checked"] == "true"
        assert target["criteria"]["1"]["selected"] is False
        assert questions["operation"]["instructions"]["rules"] in target["instructions"]["rules"]
        return {
            "model": "test",
            "answers": {
                "operation": choice(questions["operation"]["criteria"], "CLICK"),
                "click_target": choice(target["criteria"], "3"),
            },
        }

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    d = model.choose(p, "Search with free cancellation", [])
    assert d["choice"] == "e3"


@pytest.mark.parametrize("modal_open", [True, False])
def test_model_receives_observed_modal_state(monkeypatch, modal_open):
    p = page()
    p["modal_open"] = modal_open

    def post(_url, _key, body):
        assert body["state"]["page"]["modal_open"] is modal_open
        return {"model": "test", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "BLOCKED"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(p, "Confirm the pending dialog edit", [])


def test_excluding_a_forbidden_click_preserves_observed_indices_and_field_values(monkeypatch):
    p = page()
    p["actions"] = [a for a in p["actions"] if a["kind"] != "fill"]
    p["actions"][0]["value"] = "already entered"
    p["marker"] = ["same-document", "stable-controls"]
    execution = {"objective": "Apply the edit", "_last_mutation": (
        ("click", 10, "already entered"), deepcopy(p["marker"]), deepcopy(p["marker"]))}

    def post(_url, _key, body):
        assert set(body["questions"]["click_target"]["criteria"]) == {"2"}
        field, button = body["state"]["elements"]
        assert field["index"] == "1" and field["value"] == "already entered"
        assert field["operations"] == []
        assert button["index"] == "2" and button["operations"] == ["CLICK"]
        assert body["state"]["execution"] == {"objective": "Apply the edit"}
        return {"model": "test", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "CLICK"),
            "click_target": choice(["2"], "2"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(p, "Apply the edit", [], execution=execution)
    assert result["choice"] == "e3" and result["target"] == "2"
    assert len(p["actions"]) == 3  # Request filtering must not mutate the browser's observed table.


def test_model_receives_navigation_evidence_and_unmet_goal_blocked_rule(monkeypatch):
    def post(_url, _key, body):
        assert "goal remains unmet" in body["questions"]["operation"]["criteria"]["BLOCKED"].lower()
        recent = body["state"]["recent_actions"][0]
        assert recent["from_url"] == "https://example.test/list"
        assert recent["url"] == "https://example.test/article"
        return {"model": "test", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "BLOCKED"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    result = model.choose(page(), "Open the article", [{
        "action": "Article", "kind": "click", "page_changed": True,
        "from_url": "https://example.test/list", "url": "https://example.test/article",
    }])
    assert result["choice"] == "BLOCKED"  # Better evidence must not silently override the model choice.


def test_quoted_task_text_batches_observed_fields_in_one_llm_call(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    post = Mock(return_value={"choices": [{"message": {"content": '{"values":{"e1":"Zurich"}}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    context = model.text_context('Fly from "Zurich" to London', page(), [])
    assert model.field_texts(context)[0] == {"e1": "Zurich"}
    assert post.call_count == 1
    sent = json.loads(post.call_args.args[2]["messages"][1]["content"])
    assert sent["goal"] == 'Fly from "Zurich" to London'
    assert set(sent["fields"]) == {"e1"}


def test_missing_text_credential_stops_before_guessing(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_texts({"fields": {"e1": {"label": "Search"}}})


@pytest.fixture
def runner():
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots = False
    a.pending_text = None
    a.speculative_text = True
    a.text_provider = None
    a.execution_context = None
    a._text_slots = BoundedSemaphore(2)
    p = page()
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p,
        "decision": decision(),
        "goal": "Find a book",
        "history": [],
        "decisions": [],
        "prediction_calls": [],
        "status": "predicted",
        "stop_reason": None,
        "stale_retries": 0,
        "started_at": time.perf_counter(),
        "record": False,
        "text_calls": [],
    }
    a.browser = a.state["browser"]
    return a


def test_stale_decision_is_consumed_before_any_mutation(runner):
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()
    assert runner.state["decision"] is None


def test_default_selected_only_skips_helper_on_click(runner, monkeypatch):
    runner.speculative_text = False
    helper = Mock()
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(loop, "choose", Mock(return_value={**decision("e3"), "operation": "CLICK"}))
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    helper.assert_not_called()
    assert runner.state["text_calls"] == []


def test_selected_only_reuses_value_after_stale_pre_input_rejection(runner, monkeypatch):
    runner.speculative_text = False
    helper = Mock(return_value=({"e1": "book"}, {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision()))
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    runner.command("predict")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    helper.assert_called_once()
    assert set(helper.call_args.args[0]["fields"]) == {"e1"}
    assert len(runner.state["text_calls"]) == 1


def test_speculative_text_starts_before_typesafe_returns(runner, monkeypatch):
    from threading import Event

    started = Event()
    release = Event()
    def generate(_context):
        started.set()
        assert release.wait(2)
        return {"e1": "book"}, {"model": "test", "latency_ms": 10}

    helper = Mock(side_effect=generate)
    monkeypatch.setattr(loop, "field_texts", helper)

    def choose_after_prefetch(*_args):
        assert started.wait(2), "text generation should overlap TypeSafe"
        release.set()
        return decision()

    monkeypatch.setattr(loop, "choose", choose_after_prefetch)
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["browser"].act.call_args.kwargs["text"] == "book"
    assert helper.call_count == 1


def test_only_selected_target_consumes_prepared_text(runner, monkeypatch):
    runner.state["page"]["actions"].insert(1, {
        "id": "e4", "kind": "fill", "label": "Destination", "role": "textbox", "value": "", "node": 40,
    })
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    monkeypatch.setattr(loop, "field_texts", Mock(return_value=(
        {"e1": "wrong field", "e4": "chosen field"}, {"model": "test", "latency_ms": 10}
    )))
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e4")))
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["browser"].act.call_args.args[0]["id"] == "e4"
    assert runner.state["browser"].act.call_args.kwargs["text"] == "chosen field"


def test_selected_only_helper_sees_only_chosen_field(runner, monkeypatch):
    runner.speculative_text = False
    runner.state["page"]["actions"].insert(1, {
        "id": "e4", "kind": "fill", "label": "Destination", "role": "textbox", "value": "", "node": 40,
    })
    runner.state["page"]["fingerprint"] = fingerprint(runner.state["page"])
    helper = Mock(return_value=({"e4": "London"}, {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision("e4")))
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert set(helper.call_args.args[0]["fields"]) == {"e4"}
    assert runner.state["browser"].act.call_args.kwargs["text"] == "London"


def test_click_does_not_wait_for_unused_text(runner, monkeypatch):
    from threading import Event

    started, release = Event(), Event()

    def generate(_context):
        started.set()
        release.wait(2)
        return {"e1": "book"}, {"model": "test", "latency_ms": 10}

    monkeypatch.setattr(loop, "field_texts", generate)
    click_decision = {**decision("e3"), "operation": "CLICK"}
    monkeypatch.setattr(loop, "choose", Mock(return_value=click_decision))
    try:
        runner.command("predict")
        assert started.wait(2)
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
        runner.state["browser"].act.assert_called_once()
        assert not release.is_set()
        assert runner.state["text_calls"][0]["used"] is False
    finally:
        release.set()


def test_no_editable_controls_skip_text_helper(runner, monkeypatch):
    runner.state["page"]["actions"] = [a for a in runner.state["page"]["actions"] if a["kind"] != "fill"]
    helper = Mock()
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(loop, "choose", Mock(return_value={**decision("e3"), "operation": "CLICK"}))
    runner.command("predict")
    helper.assert_not_called()
    assert runner.pending_text is None


def test_failed_unused_prefetch_does_not_block_click(runner, monkeypatch):
    helper = Mock(side_effect=ValueError("bad helper response"))
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(loop, "choose", Mock(return_value={**decision("e3"), "operation": "CLICK"}))
    runner.command("predict")
    with pytest.raises(ValueError, match="bad helper response"):
        runner.pending_text[1].result(timeout=2)
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_called_once()
    assert runner.pending_text is None
    assert runner.state["text_calls"][0]["error"] == "ValueError"


def test_prefetch_threads_are_bounded_and_close_does_not_wait(runner, monkeypatch):
    from threading import Event

    two_started, release = Event(), Event()
    calls = []

    def generate(_context):
        calls.append(1)
        if len(calls) == 2:
            two_started.set()
        release.wait(2)
        return {"e1": "book"}, {"model": "test", "latency_ms": 10}

    monkeypatch.setattr(loop, "field_texts", generate)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision()))
    try:
        for index in range(2):
            runner.state["page"]["fingerprint"] = str(index)
            runner.command("predict")
        assert two_started.wait(2)
        runner.state["page"]["fingerprint"] = "third page"
        runner.command("predict")
        assert len(calls) == 2
        assert runner.pending_text[1] is None  # The saturated page defers text until selected.
        runner.close()
        runner.state["browser"].close.assert_called_once()
        assert runner.pending_text is None
    finally:
        release.set()


def test_saturated_selected_field_waits_for_slot_before_helper_call(runner, monkeypatch):
    from threading import Thread

    helper = Mock(return_value=({"e1": "book"}, {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_texts", helper)
    context = model.text_context(runner.state["goal"], runner.state["page"], [])
    runner.pending_text = (context, None, None)
    assert runner._text_slots.acquire(blocking=False)
    assert runner._text_slots.acquire(blocking=False)
    results = []
    thread = Thread(target=lambda: results.append(runner.command(
        "act", {"fingerprint": runner.state["page"]["fingerprint"]}
    )), daemon=True)
    try:
        thread.start()
        thread.join(timeout=0.02)
        helper.assert_not_called()
        runner._text_slots.release()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert len(results) == 1 and isinstance(results[0], dict)
        helper.assert_called_once()
        assert runner.state["browser"].act.call_args.kwargs["text"] == "book"
    finally:
        if thread.is_alive():
            runner._text_slots.release()
            thread.join(timeout=2)
        runner._text_slots.release()


def test_missing_selected_text_stops_before_browser_mutation(runner, monkeypatch):
    monkeypatch.setattr(loop, "field_texts", Mock(return_value=({}, {"model": "test", "latency_ms": 10})))
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision()))
    runner.command("predict")
    with pytest.raises(ValueError, match="no value for the selected field"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_not_called()


def test_generated_batch_reused_only_for_identical_retry_context(runner, monkeypatch):
    helper = Mock(return_value=({"e1": "book"}, {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision()))
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    runner.command("predict")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 1
    assert runner.state["browser"].act.call_count == 2


def test_changed_batch_context_does_not_reuse_generated_text(runner, monkeypatch):
    helper = Mock(return_value=({"e1": "book"}, {"model": "test", "latency_ms": 10}))
    monkeypatch.setattr(loop, "field_texts", helper)
    monkeypatch.setattr(loop, "choose", Mock(return_value=decision()))
    runner.state["browser"].act.side_effect = [StalePage("Changed before input"), None]
    runner.command("predict")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["page"]["text"] = "Different page context"
    runner.command("predict")
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert helper.call_count == 2


def test_observation_identity_is_part_of_text_helper_input():
    first = page()
    other = deepcopy(first)
    other["url"] = "https://example.test/other"
    assert model.text_context("Find a book", first, []) != model.text_context("Find a book", other, [])
    other = deepcopy(first)
    other["fingerprint"] = "new controls, same visible text and fields"
    assert model.text_context("Find a book", first, []) != model.text_context("Find a book", other, [])


def test_distinct_unchanged_actions_are_not_a_repetition_stop(runner):
    for selected in ("e2", "e3", "e2"):
        runner.state["decision"] = {**decision(selected), "operation": "CLICK"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"


def test_repeated_unchanged_action_has_executor_stop_reason(runner):
    for _ in range(3):
        runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "blocked"
    assert runner.state["stop_reason"] == "no_progress"


def test_action_budget_has_executor_stop_reason(runner):
    runner.state["history"] = [{} for _ in range(loop.MAX_STEPS)]
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
    with pytest.raises(ValueError, match="budget"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["stop_reason"] == "action_budget"
    runner.state["browser"].act.assert_not_called()


def test_decision_budget_has_executor_stop_reason(runner):
    runner.state["decisions"] = [{} for _ in range(loop.MAX_STEPS * 2)]
    with pytest.raises(ValueError, match="budget"):
        runner.command("predict")
    assert runner.state["status"] == "blocked" and runner.state["stop_reason"] == "model_budget"


def test_loading_waits_do_not_trigger_no_progress_stop(runner):
    for _ in range(5):
        runner.state["decision"] = decision("wait")
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert len(runner.state["history"]) == 5 and runner.state["status"] == "ready"


def test_uncertain_scroll_is_logged_and_never_retried(runner):
    runner.state["page"]["actions"].append({
        "id": "e4", "kind": "scroll_to", "label": "Reveal result", "role": "link", "node": 44,
    })
    runner.state["decision"] = {**decision("e4"), "operation": "SCROLL_TO"}
    runner.state["browser"].act.side_effect = RuntimeError("Scroll result interrupted")
    with pytest.raises(RuntimeError, match="interrupted"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["attempted"] is True
    runner.state["browser"].act.assert_called_once()
    runner.state["browser"].observe.assert_not_called()


def test_scroll_to_same_node_is_not_mutated_twice(runner):
    action = {"id": "e4", "kind": "scroll_to", "label": "Reveal result", "role": "link", "node": 44}
    runner.state["page"]["actions"].append(action)
    result = {**decision("e4"), "operation": "SCROLL_TO"}
    runner.state["decision"] = result
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["node"] == 44
    runner.state["decision"] = result
    with pytest.raises(ValueError, match="not retried"):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].act.assert_called_once()


def test_rejected_pre_input_scroll_does_not_log_execution(runner):
    runner.state["page"]["actions"].append({
        "id": "e4", "kind": "scroll_to", "label": "Reveal result", "role": "link", "node": 44,
    })
    runner.state["decision"] = {**decision("e4"), "operation": "SCROLL_TO"}
    runner.state["browser"].act.side_effect = StalePage("Target changed before scrolling")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"] == []


def test_navigation_history_preserves_source_and_result_url(runner):
    destination = deepcopy(runner.state["page"])
    destination["url"] = "https://example.test/article"
    destination["fingerprint"] = fingerprint(destination)
    runner.state["browser"].observe.return_value = destination
    runner.state["decision"] = {**decision("e3"), "operation": "CLICK"}
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    event = runner.state["history"][-1]
    assert event["from_url"] == "https://example.test/"
    assert event["url"] == "https://example.test/article" and event["page_changed"] is True


def test_attempt_is_logged_before_browser_input(runner):
    runner.state["decision"] = decision("e3")

    def input_assertion(*_args, **_kwargs):
        assert runner.state["attempts"][-1]["status"] == "issued"
        assert runner.state["history"] == []

    runner.state["browser"].act.side_effect = input_assertion
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["attempts"][-1]["status"] == "executed"


def test_unknown_receipt_is_retained_and_further_prediction_is_refused(runner):
    from jev_ultrafast.browser import ExecutionUncertain

    receipt = {"status": "outcome_unknown", "phase": "mousePressed", "error": "TimeoutError"}
    runner.state["decision"] = decision("e3")
    runner.state["browser"].act.side_effect = ExecutionUncertain(receipt)
    with pytest.raises(ExecutionUncertain):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["attempts"][-1]["receipt"] == receipt
    assert runner.state["status"] == "needs_attention"
    with pytest.raises(ValueError, match="stopped"):
        runner.command("predict")
    runner.state["browser"].act.assert_called_once()


def test_pre_input_rejection_retains_attempt_but_does_not_consume_action_budget(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].act.side_effect = StalePage("Guard rejected")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["attempts"][-1]["status"] == "rejected_before_input"
    assert runner.state["history"] == []


def test_stale_observation_preserves_executed_action(runner):
    runner.state["decision"] = decision("e3")
    runner.state["browser"].observe.side_effect = StalePage("changed")
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["history"][-1]["action"] == "Go"
    runner.state["browser"].act.assert_called_once()


def test_observation_is_one_atomic_browser_read(monkeypatch):
    import jev_ultrafast.browser as browser

    p = page()
    cdp = Mock(return_value={"result": {"value": p}})
    monkeypatch.setattr(browser, "cdp", cdp)
    actual = browser_operation({"operation": "observe", "session": "test", "screenshot": False})
    assert actual["actions"] == p["actions"]
    assert cdp.call_count == 1
    assert cdp.call_args.args[0] == "Runtime.evaluate"


@pytest.mark.parametrize("selected", ["DONE", "BLOCKED"])
def test_terminal_choice_checks_semantics_without_executing(runner, selected):
    runner.state["decision"] = decision(selected)
    runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    runner.state["browser"].fresh.assert_called_once_with(runner.state["page"], terminal=True)
    runner.state["browser"].act.assert_not_called()
    assert runner.state["status"] == selected.lower()
    assert runner.state["stop_reason"] == ("model_done" if selected == "DONE" else "model_blocked")


@pytest.mark.parametrize("selected", ["DONE", "BLOCKED"])
def test_changed_terminal_evidence_rejects_choice(runner, selected):
    runner.state["decision"] = decision(selected)
    runner.state["browser"].fresh.return_value = False
    with pytest.raises(StalePage):
        runner.command("act", {"fingerprint": runner.state["page"]["fingerprint"]})
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    runner.state["browser"].act.assert_not_called()


def test_browser_terminal_guard_uses_its_own_marker():
    from jev_ultrafast.browser import TERMINAL_MARKER, Browser

    browser = Browser.__new__(Browser)
    browser.evaluate = Mock(return_value=["semantic evidence"])
    assert browser.fresh({"terminal_marker": ["semantic evidence"]}, terminal=True)
    browser.evaluate.assert_called_once_with(TERMINAL_MARKER)
    browser.evaluate.return_value = None
    assert not browser.fresh({"terminal_marker": ["semantic evidence"]}, terminal=True)
    with pytest.raises(ValueError, match="cannot authorize"):
        browser.fresh({"terminal_marker": ["semantic evidence"]}, action={"kind": "click"}, terminal=True)


def test_scroll_to_rejects_stale_target_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act({"id": "e4", "kind": "scroll_to", "node": 44}, page())
    operation.assert_not_called()


def test_executor_rejects_a_stale_page_before_browser_input(monkeypatch):
    import jev_ultrafast.browser as browser

    b = browser.Browser.__new__(browser.Browser)
    b.fresh = Mock(return_value=False)
    operation = Mock()
    monkeypatch.setattr(browser, "browser_operation", operation)
    with pytest.raises(StalePage):
        b.act(page()["actions"][0], page(), "book")
    operation.assert_not_called()


@pytest.mark.parametrize("response", [{"exceptionDetails": {}}, {"result": {}}])
def test_interrupted_dropdown_mutation_cannot_be_retried_as_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    # A navigation can destroy the evaluation result after the change event already fired.
    if "exceptionDetails" in response:
        response["exceptionDetails"] = {"text": "Execution context destroyed"}
    cdp = Mock(return_value=response)
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(RuntimeError, match="Dropdown execution"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e1", "kind": "select", "node": 1, "value": "Design",
        }})
    assert cdp.call_count == 1


def test_scroll_to_uses_observed_node_without_click_or_text(monkeypatch):
    import jev_ultrafast.browser as browser

    cdp = Mock(return_value={"result": {"value": {"scrolled": True}}})
    monkeypatch.setattr(browser, "cdp", cdp)
    result = browser_operation({"operation": "act", "session": "test", "action": {
        "id": "e4", "kind": "scroll_to", "node": 44,
    }})
    assert result == {"executed": "e4"}
    assert cdp.call_count == 1
    assert "scrollIntoView" in cdp.call_args.kwargs["expression"]
    assert "44" in cdp.call_args.kwargs["expression"]


@pytest.mark.parametrize("response", [{"result": {}}, {"result": {"value": None}},
                                      {"result": {"value": {"scrolled": False}}}])
def test_unconfirmed_scroll_is_not_retryable_stale(monkeypatch, response):
    import jev_ultrafast.browser as browser

    monkeypatch.setattr(browser, "cdp", Mock(return_value=response))
    with pytest.raises(RuntimeError, match="not confirmed"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e4", "kind": "scroll_to", "node": 44,
        }})


def test_interrupted_scroll_cannot_be_retried_as_stale(monkeypatch):
    import jev_ultrafast.browser as browser

    monkeypatch.setattr(browser, "cdp", Mock(return_value={"exceptionDetails": {"text": "navigation"}}))
    with pytest.raises(RuntimeError, match="interrupted"):
        browser_operation({"operation": "act", "session": "test", "action": {
            "id": "e4", "kind": "scroll_to", "node": 44,
        }})


def test_fingerprint_tracks_values_and_identity_not_screenshots():
    p = page()
    other = deepcopy(p)
    other["screenshot"] = "changed"
    assert fingerprint(p) == fingerprint(other)
    other["actions"][0]["node"] = 99
    assert fingerprint(p) != fingerprint(other)


@pytest.mark.parametrize("changed", ["Departure", "Where from?", "Where to?", "year"])
def test_flight_verification_rejects_wrong_trip(changed):
    from examples.flights import verify

    actual = {
        "url": "https://www.google.com/travel/flights/search?tfs=example",
        "text": "Track prices from Zürich to London departing 2026-09-20",
        "actions": [
            {"label": k, "value": v}
            for k, v in [
                ("Change ticket type. One way", "One way"),
                ("Where from?", "Zürich"),
                ("Where to?", "London"),
                ("Departure", "Sun, Sep 20"),
                ("Nonstop flight on Sunday, September 20. Select flight", ""),
            ]
        ],
    }
    assert verify(actual)["passed"]
    if changed == "year":
        actual["text"] = actual["text"].replace("2026", "2027")
    else:
        next(a for a in actual["actions"] if a["label"] == changed)["value"] = "wrong"
    assert not verify(actual)["passed"]


@pytest.mark.parametrize(
    "content", ["Thinking: Zurich", '{"text":null}', '{"text":"Zurich","extra":true}', '{"text":123}',
                '{"values":{"unknown":"Zurich"}}', '{"values":{"e1":123}}']
)
def test_text_helper_rejects_invalid_values(monkeypatch, content):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={"choices": [{"message": {"content": content}}]}))
    with pytest.raises(ValueError, match="nothing typed"):
        model.field_texts({"fields": {"e1": {"label": "Search"}}})


def test_text_helper_omits_null_fields(monkeypatch):
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", Mock(return_value={
        "choices": [{"message": {"content": '{"values":{"e1":null}}'}}]
    }))
    assert model.field_texts({"fields": {"e1": {"label": "Search"}}})[0] == {}


def test_navigation_during_prediction_reobserves_without_action(runner):
    runner.state["browser"].fresh.side_effect = StalePage("Document navigating")
    runner.command("tick")
    assert runner.state["status"] == "ready"
    assert runner.state["decision"] is None
    assert runner.state["stale_retries"] == 1 and runner.state["stop_reason"] is None
    runner.state["browser"].act.assert_not_called()
