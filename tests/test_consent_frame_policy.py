"""Caller-owned caption policy composed with in-process modal frames: reject-type only, exact captions."""

from copy import deepcopy
from unittest.mock import Mock

import pytest
from test_frame_click_browser import child_page, parent_page

from jev_ultrafast import browser as module
from jev_ultrafast.browser import PolicyRejected
from scripts.consent_probe import ConsentOnlyBrowser
from scripts.live_safety import safe_reject


@pytest.mark.parametrize("label,allowed", [
    ("Reject all", True), ("I do not agree", True), ("Do not consent", True), ("Only necessary cookies", True),
    ("Reject all and subscribe", False), ("Accept all", False), ("I agree", False), ("Manage options", False),
    ("sign in", False), ("Reject all and pay", False), ("I do not agree and subscribe", False),
])
def test_only_exact_reject_type_captions_are_permitted(label, allowed):
    assert safe_reject(label) is allowed


def framed(monkeypatch, label):
    b = ConsentOnlyBrowser.__new__(ConsentOnlyBrowser)
    b.session, b.target = "page-session", "owned-tab"
    log = []

    def child():
        page = child_page()
        page["actions"][0]["label"] = label
        return page

    def cdp(method, *, session_id=None, **params):
        log.append(method)
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "root"}, "childFrames": [{"frame": {"id": "frame-1"}}]}}
        if method == "Target.getTargets":
            return {"targetInfos": []}
        if method == "DOM.getFrameOwner":
            return {"backendNodeId": 77}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "owner"}}
        if method == "Runtime.callFunctionOn":
            if params["functionDeclaration"] == module.FRAME_OWNER_ORIGIN:
                return {"result": {"value": {"x": 40.0, "y": 60.0}}}
            return {"result": {"value": {"valid": True}}}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": 9}
        if method == "Runtime.evaluate" and params.get("contextId") == 9:
            return {"result": {"value": child()}}
        return {}

    monkeypatch.setattr(module, "cdp", cdp)
    monkeypatch.setattr(module, "browser_operation", lambda request, **kwargs: deepcopy(parent_page()))
    page = b.observe(screenshot=False)
    return b, page, next(a for a in page["actions"] if a.get("frame_id")), log


def test_reject_type_caption_in_a_same_site_modal_frame_is_permitted(monkeypatch):
    b, page, action, _ = framed(monkeypatch, "I do not agree")
    b.validate_action(action, page)  # Does not raise: caller policy and frame gates both pass.


@pytest.mark.parametrize("label", ["Reject all and subscribe", "Accept all", "sign in"])
def test_pay_accept_and_account_captions_are_rejected_before_any_input(monkeypatch, label):
    b, page, action, log = framed(monkeypatch, label)
    with pytest.raises(PolicyRejected):
        b.validate_action(action, page)
    b.fresh = Mock(side_effect=AssertionError("freshness must not run for a vetoed action"))
    receipt = b.execute(action, page)
    assert receipt["status"] == "rejected_by_policy" and receipt["input_started"] is False
    assert not any(m.startswith("Input.") for m in log)
