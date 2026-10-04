"""Offline contracts for same-process (isolated world) modal frame clicks; mirrors the OOPIF contracts."""

from copy import deepcopy
from unittest.mock import Mock

from test_frame_click_browser import child_page, parent_page

from jev_ultrafast import browser as module


def observed(monkeypatch, *, separate=False, origin=(40.0, 60.0)):
    b = module.Browser.__new__(module.Browser)
    b.session, b.target, b.frame_clicks_enabled = "page-session", "owned-tab", True
    log = []

    def cdp(method, *, session_id=None, **params):
        log.append((method, session_id, params))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "root"}, "childFrames": [{"frame": {"id": "frame-1"}}]}}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "iframe", "targetId": "frame-1", "parentFrameId": "root"}]
                    if separate else []}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 77}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "owner"}}
        if method == "Runtime.callFunctionOn":
            if params["functionDeclaration"] == module.FRAME_OWNER_ORIGIN:
                return {"result": {"value": {"x": origin[0], "y": origin[1]}}}
            return {"result": {"value": {"valid": True}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 9}
        if method == "Runtime.evaluate" and params.get("contextId") == 9:
            return {"result": {"value": child_page()}}
        return {}

    monkeypatch.setattr(module, "cdp", cdp)
    monkeypatch.setattr(module, "browser_operation", lambda request, **kwargs: deepcopy(parent_page()))
    return b, log


def test_same_process_frame_is_read_through_an_isolated_world_not_a_child_session(monkeypatch):
    b, log = observed(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("frame_id"))
    assert action["label"] == "Reject" and action["node"] < 0 and page["unindexed_modal_frames"] == 0
    assert not any(m == "Target.attachToTarget" for m, _, _ in log)
    # Moving the frame is a new decision, so its origin is part of the marker.
    assert {"x": 40.0, "y": 60.0} in page["identity_marker"][f'{action["node"]}:click']


def test_a_frame_with_its_own_target_is_never_treated_as_same_process(monkeypatch):
    b, _ = observed(monkeypatch, separate=True)
    assert not b._frame_owner_valid("frame-1", 77, None, in_process=True)


def test_click_resolves_in_the_world_and_dispatches_on_the_page_session_composed(monkeypatch):
    b, _ = observed(monkeypatch)
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("frame_id"))
    b.fresh = Mock(return_value=True)
    b._frame_owner_valid = Mock(return_value=True)
    calls = []

    def act(request, *, transport):
        assert request["session"] == "page-session" and request["action"]["node"] == 1
        transport("Runtime.evaluate", _phase="target_resolution", expression="1", returnByValue=True)
        for phase in ("mousePressed", "mouseReleased"):
            transport("Input.dispatchMouseEvent", _phase=phase, _input=True,
                      type=phase, x=43, y=27, button="left", clickCount=1)

    def cdp(method, *, session_id=None, **params):
        calls.append((method, session_id, params.get("contextId"), params.get("x"), params.get("y")))
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": {"x": 40.0, "y": 60.0}}}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "owner"}}
        return {} if method.startswith("Input.") else {"result": {"value": {}}}

    monkeypatch.setattr(module, "browser_operation", act)
    monkeypatch.setattr(module, "cdp", cdp)
    receipt = b.execute(action, page)
    assert receipt["status"] == "executed" and receipt["input_started"] is True
    assert ("Runtime.evaluate", "page-session", 9, None, None) in calls
    inputs = [c for c in calls if c[0] == "Input.dispatchMouseEvent"]
    assert inputs == [("Input.dispatchMouseEvent", "page-session", None, 83.0, 87.0)] * 2


def test_moved_frame_is_stale_and_sends_no_input(monkeypatch):
    b, _ = observed(monkeypatch, origin=(40.0, 60.0))
    page = b.observe(screenshot=False)
    action = next(a for a in page["actions"] if a.get("frame_id"))
    moved, _ = observed(monkeypatch, origin=(77.0, 60.0))
    key = f'{action["node"]}:click'
    assert moved.observe(screenshot=False)["identity_marker"][key] != page["identity_marker"][key]
