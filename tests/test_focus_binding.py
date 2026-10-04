"""Native fill focus binding. Offline CDP only; no browser/model requests."""

import json
import subprocess

import pytest
from test_execution import client


def test_click_focus_redirect_stops_before_keyboard_and_locks_execution(monkeypatch):
    b, cdp, action, page = client(monkeypatch)
    action.update(kind="fill", role="textbox")
    focus = [1]
    commands = []

    def reply(method, **params):
        commands.append(method)
        if method == "Runtime.evaluate":
            if len(commands) == 1:
                return {"result": {"value": {"x": 40, "y": 50}}}
            return {"result": {"value": focus[0] == action["node"]}}
        if method == "Input.dispatchMouseEvent" and params["type"] == "mouseReleased":
            focus[0] = 99  # Application callback redirects to an unobserved field.
        return {}

    cdp.side_effect = reply
    receipt = b.execute(action, page, "private prepared value")
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"] is True
    assert receipt["phase"] == "focus_after_activation"
    assert commands == ["Runtime.evaluate", "Input.dispatchMouseEvent", "Input.dispatchMouseEvent",
                        "Runtime.evaluate"]
    assert "private" not in repr(receipt)
    assert b.execute(action, page, "private prepared value") == receipt
    assert len(commands) == 4 and len(b.receipts) == 1


def test_keyboard_handler_focus_redirect_stops_before_insertion(monkeypatch):
    b, cdp, action, page = client(monkeypatch)
    action.update(kind="fill", role="textbox")
    focus = [1]
    evaluations = []
    commands = []

    def reply(method, **params):
        commands.append(method)
        if method == "Runtime.evaluate":
            evaluations.append(params["expression"])
            value = {"x": 40, "y": 50} if len(evaluations) == 1 else focus[0] == 1
            return {"result": {"value": value}}
        if method == "Input.dispatchKeyEvent" and params["type"] == "keyUp":
            focus[0] = 99
        return {}

    cdp.side_effect = reply
    receipt = b.execute(action, page, "private prepared value")
    assert receipt["status"] == "outcome_unknown" and receipt["phase"] == "focus_before_insert"
    assert commands.count("Input.dispatchKeyEvent") == 2 and "Input.insertText" not in commands
    assert evaluations[1] == evaluations[2]
    assert "private" not in repr(receipt)


@pytest.mark.parametrize("phase", ["focus_after_activation", "focus_before_insert"])
@pytest.mark.parametrize("value", [False, None, 1, "true", {"private": "value"}, "timeout", "exception"])
def test_unavailable_or_nonboolean_focus_proof_is_uncertain_not_pre_input_rejection(monkeypatch, phase, value):
    failure = TimeoutError("private exception") if value == "timeout" else (
        {"exceptionDetails": {"text": "private exception"}} if value == "exception" else
        {"result": {"value": value}})
    replies = [{"result": {"value": {"x": 40, "y": 50}}}, {}, {}]
    if phase == "focus_before_insert":
        replies += [{"result": {"value": True}}, {}, {}]
    replies.append(failure)
    b, cdp, action, page = client(monkeypatch, replies)
    action["kind"] = "fill"
    receipt = b.execute(action, page, "private prepared value")
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"] is True
    assert receipt["phase"] == phase
    assert not any(c.args[0] == "Input.insertText" for c in cdp.call_args_list)
    if phase == "focus_after_activation":
        assert not any(c.args[0] == "Input.dispatchKeyEvent" for c in cdp.call_args_list)
    assert b.execute(action, page, "private prepared value") == receipt
    assert len(b.receipts) == 1 and cdp.call_count == len(replies)
    assert "private" not in repr(receipt)


def test_focus_loss_reaches_controller_as_uncertain_without_replan_or_replay(monkeypatch):
    from unittest.mock import Mock

    from test_objective import FakeBrowser, chosen, search_plan

    from jev_ultrafast import Check, ObjectiveAgent
    from jev_ultrafast import agent as loop
    from jev_ultrafast import browser as module

    class NativeFillBrowser(FakeBrowser):
        session = "offline-session"
        call = module.Browser.call
        execute = module.Browser.execute
        act = module.Browser.act
        validate_action = module.Browser.validate_action

    cdp = Mock(side_effect=[{"result": {"value": {"x": 40, "y": 50}}}, {}, {},
                           {"result": {"value": False}}])
    chooser = Mock(return_value=chosen("e1"))
    planner = Mock(side_effect=AssertionError("Unknown input cannot trigger replanning"))
    monkeypatch.setattr(module, "cdp", cdp)
    monkeypatch.setattr(loop, "Browser", NativeFillBrowser)
    monkeypatch.setattr(loop, "choose", chooser)
    with ObjectiveAgent("https://example.test/", "Enter Ada Lovelace in Search",
                        checks=(Check("value", "Ada Lovelace", label="Search"),),
                        prepared_plan=search_plan(), planner=planner) as controller:
        state = controller.command()
        assert state["status"] == "needs_attention" and state["stop_reason"] == "execution_error"
        assert state["history"] == [] and len(state["attempts"]) == 1
        assert state["attempts"][0]["status"] == "outcome_unknown"
        assert state["attempts"][0]["receipt"]["phase"] == "focus_after_activation"
        assert state["events"][-1]["error"] == "ExecutionUncertain"
        assert state["events"][-1]["phase"] == "execution"
        assert len(state["decisions"]) == len(state["prediction_calls"]) == 1
        browser = controller.browser
    assert not browser.closed
    chooser.assert_called_once()
    planner.assert_not_called()
    assert cdp.call_count == 4


def test_bound_field_typing_succeeds_and_actual_javascript_rejects_unsafe_receivers(monkeypatch):
    replies = [{"result": {"value": {"x": 40, "y": 50}}}, {}, {}, {"result": {"value": True}},
               {}, {}, {"result": {"value": True}}, {}]
    b, cdp, action, page = client(monkeypatch, replies)
    action["kind"] = "fill"
    receipt = b.execute(action, page, "private prepared value")
    assert receipt["status"] == "executed"
    assert [c["phase"] for c in receipt["calls"]] == [
        "target_resolution", "mousePressed", "mouseReleased", "focus_after_activation",
        "selectAllDown", "selectAllUp", "focus_before_insert", "insertText"]
    assert all(set(c) == {"method", "phase", "status", "ms"} for c in receipt["calls"])
    assert cdp.call_args_list[-1].kwargs["text"] == "private prepared value"
    script = cdp.call_args_list[3].kwargs["expression"]
    assert "private" not in script and "private" not in repr(receipt)
    assert script == cdp.call_args_list[6].kwargs["expression"]
    cases = [
        ({}, True), ({"type": "search"}, True), ({"type": "number"}, True),
        ({"tagName": "TEXTAREA"}, True), ({"tagName": "DIV", "contentEditable": True}, True),
        ({"focus": False}, False), ({"replacement": True}, False),
        ({"missing": True}, False), ({"connected": False}, False),
        ({"visible": False}, False), ({"readonly": True}, False),
        ({"attrs": {"aria-readonly": "true"}}, False), ({"disabled": True}, False),
        ({"coveredAncestor": True}, False), ({"tagName": "DIV"}, False),
        ({"type": "password"}, False), ({"type": "file"}, False), ({"type": "hidden"}, False),
        ({"type": "password", "contentEditable": True}, False),
        ({"type": "file", "contentEditable": True}, False), ({"type": "date"}, False),
        ({"tagName": "IFRAME", "contentEditable": True}, False),
        ({"tagName": "OBJECT", "contentEditable": True}, False),
        ({"tagName": "DIV", "contentEditable": True, "shadowFocus": True}, False),
    ]
    runner = """const vm=require('vm'), data=JSON.parse(require('fs').readFileSync(0,'utf8'));
const result=data.cases.map(o=>{const e={tagName:o.tagName||'INPUT',type:o.type||'text',
  isConnected:o.connected!==false,readOnly:!!o.readonly,isContentEditable:!!o.contentEditable,
  shadowRoot:o.shadowFocus?{activeElement:{}}:null,
  getAttribute:k=>o.attrs?.[k]??null,matches:()=>!!o.disabled,
  closest:()=>o.coveredAncestor?{}:null,checkVisibility:()=>o.visible!==false};
  return vm.runInNewContext(data.script,{window:{__jevFast:{nodes:new Map(o.missing?[]:[[1,e]])}},
    document:{activeElement:o.focus===false?{}:o.replacement?{...e}:e}});});console.log(JSON.stringify(result));"""
    result = subprocess.run(["node", "-e", runner], input=json.dumps({"script": script,
                            "cases": [c for c, _ in cases]}), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == [expected for _, expected in cases]
