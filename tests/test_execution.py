"""Execution receipts describe transport certainty, never goal completion. Offline CDP."""

from unittest.mock import Mock

import pytest

from jev_ultrafast import browser as module


def client(monkeypatch, replies=None):
    b = module.Browser.__new__(module.Browser)
    b.session = "owned-session"
    b.fresh = Mock(return_value=True)
    cdp = Mock(side_effect=replies or [{"result": {"value": {"x": 40, "y": 50}}}, {}, {}])
    monkeypatch.setattr(module, "cdp", cdp)
    action = {"id": "e1", "kind": "click", "node": 1, "label": "Open menu"}
    return b, cdp, action, {"actions": [action]}


def test_stale_observed_target_is_rejected_before_input_without_transport_retry(monkeypatch):
    b, cdp, action, page = client(monkeypatch)
    b.fresh.return_value = False
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_before_input" and not receipt["input_started"]
    assert receipt["phase"] == "freshness"
    assert len(b.receipts) == 1 and b.receipts[0] == receipt
    cdp.assert_not_called()


def test_success_traces_each_call_without_page_scripts_text_or_coordinates(monkeypatch):
    b, cdp, action, page = client(monkeypatch)
    receipt = b.execute(action, page)
    assert receipt["status"] == "executed" and receipt["input_started"]
    assert [c["phase"] for c in receipt["calls"]] == ["target_resolution", "mousePressed", "mouseReleased"]
    assert all(set(c) == {"method", "phase", "status", "ms"} for c in receipt["calls"])
    assert cdp.call_count == 3


def test_target_read_timeout_is_known_pre_input_rejection(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [TimeoutError("secret page data")])
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_before_input" and not receipt["input_started"]
    assert receipt["phase"] == "target_resolution" and receipt["error"] == "TimeoutError"
    assert cdp.call_count == 1
    assert "secret" not in str(receipt)


def test_pressed_timeout_is_unknown_and_release_is_not_issued(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"x": 40, "y": 50}}}, TimeoutError()])
    receipt = b.execute(action, page)
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"]
    assert receipt["phase"] == "mousePressed"
    assert cdp.call_count == 2 and receipt["calls"][-1]["status"] == "error"


def test_released_timeout_is_unknown_and_not_retried(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"x": 40, "y": 50}}}, {}, TimeoutError()])
    receipt = b.execute(action, page)
    assert receipt["status"] == "outcome_unknown" and receipt["phase"] == "mouseReleased"
    assert cdp.call_count == 3


@pytest.mark.parametrize("kind", ["select", "scroll_to"])
def test_javascript_mutation_timeout_is_unknown_and_not_retried(monkeypatch, kind):
    b, cdp, action, page = client(monkeypatch, [TimeoutError()])
    action["kind"] = kind
    action["value"] = "design"
    receipt = b.execute(action, page)
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"]
    assert receipt["phase"] == kind and cdp.call_count == 1


@pytest.mark.parametrize("kind", ["click", "fill", "select", "scroll_to"])
def test_code_owned_target_rejection_proves_no_input_for_disabled_detached_or_covered(monkeypatch, kind):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"rejected": True}}}])
    action["kind"] = kind
    receipt = b.execute(action, page, "Ada" if kind == "fill" else None)
    assert receipt["status"] == "rejected_before_input" and not receipt["input_started"]
    assert cdp.call_count == 1


@pytest.mark.parametrize("invalid", ["node", "id", "kind", "duplicate", "text", "non_fill_text"])
def test_invalid_or_unobserved_actions_are_rejected_without_transport(monkeypatch, invalid):
    b, cdp, action, page = client(monkeypatch)
    action = dict(action)
    text = None
    if invalid in {"node", "id", "kind"}:
        action[invalid] = "invented"
    elif invalid == "duplicate":
        page["actions"].append(dict(action))
    elif invalid == "text":
        action["kind"] = "fill"
        page["actions"] = [dict(action)]
        text = " "
    else:
        text = "not for clicks"
    receipt = b.execute(action, page, text)
    assert receipt["status"] == "rejected_before_input" and receipt["phase"] == "validation"
    cdp.assert_not_called()


def test_insert_text_timeout_cannot_trigger_another_click_or_typing(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [
        {"result": {"value": {"x": 40, "y": 50}}}, {}, {}, {}, {}, TimeoutError(),
    ])
    action["kind"] = "fill"
    receipt = b.execute(action, page, "Ada")
    assert receipt["status"] == "outcome_unknown" and receipt["phase"] == "insertText"
    assert cdp.call_count == 6


def test_keyboard_interrupt_retains_unknown_receipt_and_stops_input(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"x": 40, "y": 50}}}, KeyboardInterrupt()])
    with pytest.raises(KeyboardInterrupt):
        b.execute(action, page)
    assert b.receipts[-1]["status"] == "outcome_unknown" and b.receipts[-1]["error"] == "KeyboardInterrupt"
    assert cdp.call_count == 2


def test_compatibility_facade_raises_with_unknown_receipt(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"x": 40, "y": 50}}}, TimeoutError()])
    with pytest.raises(module.ExecutionUncertain) as caught:
        b.act(action, page)
    assert caught.value.receipt == b.receipts[-1]
    assert cdp.call_count == 2


def test_unknown_outcome_locks_execution_even_with_a_new_observed_action(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"x": 40, "y": 50}}}, TimeoutError()])
    first = b.execute(action, page)
    other = {**action, "id": "e2", "node": 2}
    assert b.execute(other, {"actions": [other]}) == first
    assert cdp.call_count == 2 and len(b.receipts) == 1


@pytest.mark.parametrize("reply", [None, False, {"error": {"message": "secret"}}, {"unexpected": "secret"}])
def test_malformed_native_ack_is_unknown_and_stops_remaining_input(monkeypatch, reply):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"x": 40, "y": 50}}}, reply])
    receipt = b.execute(action, page)
    assert receipt["status"] == "outcome_unknown" and receipt["phase"] == "mousePressed"
    assert cdp.call_count == 2 and "secret" not in str(receipt)


def test_returned_receipt_annotation_cannot_change_execution_certainty(monkeypatch):
    b, cdp, action, page = client(monkeypatch, [{"result": {"value": {"x": 40, "y": 50}}}, TimeoutError()])
    receipt = b.execute(action, page)
    receipt["status"] = "executed"
    with pytest.raises(module.ExecutionUncertain):
        b.act(action, page)
    assert cdp.call_count == 2


def test_snapshot_and_post_input_reads_are_traced_without_reissuing_input(monkeypatch):
    b, cdp, action, _ = client(monkeypatch, [{}, {"result": {"value": {
        "url": "https://example.test", "text": "", "actions": [], "scroll": {},
    }}}])
    b.after_input = action
    b.observe(screenshot=False)
    assert [c["phase"] for c in b.cdp_calls] == ["post_input_wait", "snapshot"]
    assert cdp.call_count == 2
