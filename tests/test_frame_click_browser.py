"""Offline CDP/OOPIF contracts: only owned modal buttons become frame-scoped clicks."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from jev_ultrafast import browser as module


def parent_page():
    return {"url": "https://parent.test/", "title": "Parent", "text": "",
            "document_id": 123, "modal_open": True, "modal_label": "Consent message",
            "unindexed_modal_frames": 1, "actions": [{"id": "wait", "kind": "wait", "label": "Wait"}],
            "controls": [], "scroll": {}, "marker": ["parent"],
            "semantic_marker": ["parent"], "terminal_marker": ["parent"],
            "identity_marker": {}}


def child_page():
    return {"url": "https://child.test/?session_handoff=private-token",
            "document_id": 456, "modal_open": False, "modal_label": "",
            "actions": [{"id": "e1", "kind": "click", "role": "button", "node": 1,
                         "label": "Reject", "value": "", "rect": {"x": 8, "y": 8, "w": 70, "h": 24}}],
            "omitted_actions": 0, "omitted_controls": 0,
            "identity_marker": {"1:click": [456, "https://child.test/?session_handoff=private-token", 1]},
            "semantic_marker": ["child", "https://child.test/?session_handoff=private-token"],
            "terminal_marker": ["child", "https://child.test/?session_handoff=private-token"]}


def observed_browser(monkeypatch, *, parent_id="root", owner_valid=True):
    b = module.Browser.__new__(module.Browser)
    b.session = "parent-session"
    b.target = "owned-tab"
    b.frame_clicks_enabled = True
    replies = []

    def cdp(method, *, session_id=None, **params):
        replies.append((method, session_id))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "root"}}}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "iframe", "targetId": "frame-1",
                                     "parentFrameId": parent_id,
                                     "url": "https://child.test/?session_handoff=private-token"}]}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 77}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "owner"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"valid": owner_valid}}}
        if method == "Target.attachToTarget":
            return {"sessionId": "child-session"}
        if method == "Runtime.evaluate" and session_id == "child-session":
            return {"result": {"value": child_page()}}
        raise AssertionError((method, session_id))

    monkeypatch.setattr(module, "cdp", cdp)
    monkeypatch.setattr(module, "browser_operation", lambda request, **kwargs: deepcopy(parent_page()))
    return b, replies


def test_owned_frame_click_is_observed_with_unique_node_and_no_token_in_model_state(monkeypatch):
    b, calls = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    buttons = [a for a in page["actions"] if a.get("kind") == "click"]
    assert len(buttons) == 1
    action = buttons[0]
    assert action["label"] == "Reject" and action["node"] < 0
    assert action["frame_id"] == "frame-1" and action["frame_node"] == 1
    assert page["unindexed_modal_frames"] == 0
    assert page["semantic_marker"] != parent_page()["semantic_marker"]
    assert page["identity_marker"][f'{action["node"]}:click'] is not None
    assert "private-token" not in json.dumps(page)
    assert ("Runtime.evaluate", "child-session") in calls


@pytest.mark.parametrize("parent_id,owner_valid", [("other-root", True), ("root", False)])
def test_unowned_or_unhittable_frame_remains_unindexed(monkeypatch, parent_id, owner_valid):
    b, calls = observed_browser(monkeypatch, parent_id=parent_id, owner_valid=owner_valid)
    page = b.observe(screenshot=False)
    assert page["unindexed_modal_frames"] == 1
    assert not any(a["kind"] == "click" for a in page["actions"])
    assert ("Target.attachToTarget", None) not in calls


def test_frame_click_routes_resolution_and_input_only_to_bound_child_session(monkeypatch):
    b, _ = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    b.fresh = Mock(return_value=True)
    b._frame_owner_valid = Mock(return_value=True)
    routed = []

    def act(request, *, transport):
        routed.append((request["session"], request["action"]["node"]))
        transport("Runtime.evaluate", _phase="target_resolution", expression="1", returnByValue=True)
        for phase in ("mousePressed", "mouseReleased"):
            transport("Input.dispatchMouseEvent", _phase=phase, _input=True,
                      type=phase, x=43, y=27, button="left", clickCount=1)
        return {"executed": request["action"]["id"]}

    calls = []

    def cdp(method, *, session_id=None, **params):
        calls.append((method, session_id))
        return {"result": {"value": {"x": 43, "y": 27}}} if method == "Runtime.evaluate" else {}

    monkeypatch.setattr(module, "browser_operation", act)
    monkeypatch.setattr(module, "cdp", cdp)
    receipt = b.execute(action, page)
    assert receipt["status"] == "executed" and receipt["input_started"] is True
    assert routed == [("child-session", 1)]
    assert calls == [("Runtime.evaluate", "child-session"),
                     ("Input.dispatchMouseEvent", "child-session"),
                     ("Input.dispatchMouseEvent", "child-session")]


def test_changed_frame_owner_rejects_before_any_child_input(monkeypatch):
    b, _ = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    b.fresh = Mock(return_value=True)
    b._frame_owner_valid = Mock(return_value=False)
    transport = Mock(side_effect=AssertionError("No browser input"))
    monkeypatch.setattr(module, "cdp", transport)
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    transport.assert_not_called()


@pytest.mark.parametrize("change", ["navigation", "geometry"])
def test_frame_navigation_or_movement_invalidates_click_before_input(monkeypatch, change):
    b, calls = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    original = child_page

    def changed():
        state = original()
        if change == "navigation":
            state["identity_marker"]["1:click"][0] = 999
        else:
            state["actions"][0]["rect"]["x"] = 80
        return state

    monkeypatch.setattr("test_frame_click_browser.child_page", changed)
    assert b.fresh(page, action) is False
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_before_input" and not receipt["input_started"]
    assert not any(method.startswith("Input.") for method, _ in calls)


def test_parent_url_change_invalidates_frame_click(monkeypatch):
    b, calls = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")

    def changed_parent(request, **_kwargs):
        state = parent_page()
        state["url"] = "https://parent.test/other"
        return state

    monkeypatch.setattr(module, "browser_operation", changed_parent)
    assert b.fresh(page, action) is False
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_before_input" and not receipt["input_started"]
    assert not any(method.startswith("Input.") for method, _ in calls)


def test_frame_point_becoming_covered_after_resolution_prevents_input(monkeypatch):
    b, _ = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    b.fresh = Mock(return_value=True)
    b._frame_owner_valid = Mock(side_effect=[True, False])
    calls = []

    def cdp(method, *, session_id=None, **params):
        calls.append((method, session_id))
        return {"result": {"value": {"x": 43, "y": 27}}}

    def act(request, *, transport):
        transport("Runtime.evaluate", _phase="target_resolution", expression="1", returnByValue=True)
        transport("Input.dispatchMouseEvent", _phase="mousePressed", _input=True,
                  type="mousePressed", x=43, y=27, button="left", clickCount=1)
        raise AssertionError("Covered target must never receive input")

    monkeypatch.setattr(module, "browser_operation", act)
    monkeypatch.setattr(module, "cdp", cdp)
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    assert calls == [("Runtime.evaluate", "child-session")]
    assert b._frame_owner_valid.call_count == 2


def test_target_rejection_after_input_remains_unknown_and_locked(monkeypatch):
    b, _ = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    b.fresh = Mock(return_value=True)
    b._frame_owner_valid = Mock(return_value=True)
    calls = []

    def cdp(method, *, session_id=None, **params):
        calls.append((method, session_id))
        return {}

    def act(request, *, transport):
        transport("Input.dispatchMouseEvent", _phase="mousePressed", _input=True,
                  type="mousePressed", x=43, y=27, button="left", clickCount=1)
        raise module._TargetRejected("Target may have moved after the press")

    monkeypatch.setattr(module, "browser_operation", act)
    monkeypatch.setattr(module, "cdp", cdp)
    receipt = b.execute(action, page)
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"] is True
    assert b.execute(action, page)["request_id"] == receipt["request_id"]
    assert calls == [("Input.dispatchMouseEvent", "child-session")]


def test_child_input_timeout_locks_execution_without_a_release_or_replay(monkeypatch):
    b, _ = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    b.fresh = Mock(return_value=True)
    b._frame_owner_valid = Mock(return_value=True)
    calls = []

    def cdp(method, *, session_id=None, **params):
        calls.append((method, session_id))
        if method == "Input.dispatchMouseEvent":
            raise TimeoutError("Child press acknowledgment lost")
        return {"result": {"value": {"x": 43, "y": 27}}}

    def act(request, *, transport):
        transport("Runtime.evaluate", _phase="target_resolution", expression="1", returnByValue=True)
        transport("Input.dispatchMouseEvent", _phase="mousePressed", _input=True,
                  type="mousePressed", x=43, y=27, button="left", clickCount=1)
        raise AssertionError("No mouse release or replay")

    monkeypatch.setattr(module, "browser_operation", act)
    monkeypatch.setattr(module, "cdp", cdp)
    receipt = b.execute(action, page)
    assert receipt["status"] == "outcome_unknown" and receipt["phase"] == "mousePressed"
    assert calls == [("Runtime.evaluate", "child-session"),
                     ("Input.dispatchMouseEvent", "child-session")]
    assert b.execute(action, page)["request_id"] == receipt["request_id"]
    assert len(calls) == 2


def test_default_browser_never_executes_an_untrusted_frame_annotation(monkeypatch):
    b, _ = observed_browser(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a["kind"] == "click")
    b.frame_clicks_enabled = False
    b.fresh = Mock(side_effect=AssertionError("Frame input is not offered by default"))
    transport = Mock(side_effect=AssertionError("No browser input"))
    monkeypatch.setattr(module, "cdp", transport)
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    b.fresh.assert_not_called()
    transport.assert_not_called()
