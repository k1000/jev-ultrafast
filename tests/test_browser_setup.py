"""Browser bootstrap has bounded reads and no navigation retries; all CDP is fake."""

from unittest.mock import Mock

import pytest

from jev_ultrafast import browser


def setup_cdp(monkeypatch, pages=None, fail=None):
    pages = iter(pages or [{"url": "https://example.test/", "ready": "complete"}])

    def reply(method, **kwargs):
        if fail:
            failure = fail(method)
            if failure is not None:
                return failure
        if method == "Target.createTarget":
            return {"targetId": "owned-tab"}
        if method == "Target.attachToTarget":
            return {"sessionId": "owned-session"}
        if method == "Runtime.evaluate":
            return {"result": {"value": next(pages)}}
        return {}

    cdp = Mock(side_effect=reply)
    monkeypatch.setattr(browser, "cdp", cdp)
    monkeypatch.setattr(browser, "ensure_daemon", Mock())
    monkeypatch.setattr(browser.time, "sleep", Mock())
    return cdp


def test_setup_waits_past_complete_blank_document_and_allows_redirect(monkeypatch):
    cdp = setup_cdp(monkeypatch, [
        {"url": "about:blank", "ready": "complete"},
        {"url": "https://example.test/", "ready": "loading"},
        {"url": "https://example.test/redirected", "ready": "complete"},
    ])
    b = browser.Browser("https://example.test/")
    assert sum(c.args[0] == "Runtime.evaluate" for c in cdp.call_args_list) == 3
    navigation = [c for c in cdp.call_args_list if c.args[0] == "Page.navigate"]
    assert len(navigation) == 1 and navigation[0].kwargs["_response_timeout"] == 15
    b.close()


def test_navigation_rejection_is_phase_labelled_and_owned_tab_closed(monkeypatch):
    cdp = setup_cdp(monkeypatch, fail=lambda m: {"errorText": "net::ERR_FAILED"} if m == "Page.navigate" else None)
    with pytest.raises(browser.BrowserSetupError) as caught:
        browser.Browser("https://example.test/")
    assert caught.value.phase == "navigate" and caught.value.cause_type == "RuntimeError"
    assert [c.kwargs["targetId"] for c in cdp.call_args_list if c.args[0] == "Target.closeTarget"] == ["owned-tab"]
    assert sum(c.args[0] == "Page.navigate" for c in cdp.call_args_list) == 1


def test_setup_failure_retains_original_cause_when_cleanup_also_fails(monkeypatch):
    def fail(method):
        if method == "Target.attachToTarget":
            raise TimeoutError("attach timeout")
        if method == "Target.closeTarget":
            raise ValueError("close failure")

    setup_cdp(monkeypatch, fail=fail)
    with pytest.raises(browser.BrowserSetupError) as caught:
        browser.Browser("https://example.test/")
    assert caught.value.phase == "attach" and caught.value.cause_type == "TimeoutError"
    assert caught.value.cleanup_error == "ValueError"
    assert caught.value.retained_target == "owned-tab"
    assert isinstance(caught.value.__cause__, TimeoutError)


def test_load_deadline_is_not_silently_accepted_as_ready(monkeypatch):
    cdp = setup_cdp(monkeypatch)
    monkeypatch.setattr(browser.time, "monotonic", Mock(side_effect=[0, 16]))
    with pytest.raises(browser.BrowserSetupError) as caught:
        browser.Browser("https://example.test/")
    assert caught.value.phase == "document_ready" and caught.value.cause_type == "TimeoutError"
    assert sum(c.args[0] == "Page.navigate" for c in cdp.call_args_list) == 1


def test_transient_destroyed_context_only_retries_readiness_read(monkeypatch):
    first = True

    def fail(method):
        nonlocal first
        if method == "Runtime.evaluate" and first:
            first = False
            return {"exceptionDetails": {"text": "Context destroyed during navigation"}}

    cdp = setup_cdp(monkeypatch, fail=fail)
    b = browser.Browser("https://example.test/")
    assert sum(c.args[0] == "Runtime.evaluate" for c in cdp.call_args_list) == 2
    assert sum(c.args[0] == "Page.navigate" for c in cdp.call_args_list) == 1
    b.close()


def test_transient_readiness_ipc_timeout_only_retries_the_read(monkeypatch):
    from browser_harness.helpers import _IPCResponseTimeout

    first = True

    def fail(method):
        nonlocal first
        if method == "Runtime.evaluate" and first:
            first = False
            raise _IPCResponseTimeout("Readiness reply delayed")

    cdp = setup_cdp(monkeypatch, fail=fail)
    b = browser.Browser("https://example.test/")
    assert sum(c.args[0] == "Runtime.evaluate" for c in cdp.call_args_list) == 2
    assert sum(c.args[0] == "Page.navigate" for c in cdp.call_args_list) == 1
    assert [c["status"] for c in b.cdp_calls if c["method"] == "Runtime.evaluate"] == ["error", "returned"]
    assert not any(c.args[0].startswith("Input.") for c in cdp.call_args_list)
    b.close()


def test_readiness_timeout_is_capped_by_remaining_deadline(monkeypatch):
    from browser_harness.helpers import _IPCResponseTimeout

    cdp = setup_cdp(monkeypatch, pages=[{"url": "https://example.test/", "ready": "loading"}])
    reply = cdp.side_effect
    clock, reads = [0.], [0]
    monkeypatch.setattr(browser.time, "monotonic", lambda: clock[0])

    def delayed(method, **kwargs):
        if method == "Runtime.evaluate":
            reads[0] += 1
            if reads[0] == 3:
                clock[0] += 4
                return reply(method, **kwargs)
            clock[0] += kwargs.get("_response_timeout", 5)
            raise _IPCResponseTimeout("Read did not return")
        return reply(method, **kwargs)

    cdp.side_effect = delayed
    with pytest.raises(browser.BrowserSetupError) as caught:
        browser.Browser("https://example.test/")
    assert caught.value.phase == "document_ready" and caught.value.cause_type == "TimeoutError"
    evaluations = [c for c in cdp.call_args_list if c.args[0] == "Runtime.evaluate"]
    assert [c.kwargs.get("_response_timeout") for c in evaluations] == [5, 5, 5, 1]
    assert clock[0] == 15 and sum(c.args[0] == "Page.navigate" for c in cdp.call_args_list) == 1
    assert sum(c.args[0] == "Target.closeTarget" for c in cdp.call_args_list) == 1


def test_navigation_timeout_is_not_replayed(monkeypatch):
    def fail(method):
        if method == "Page.navigate":
            raise TimeoutError("navigation IPC response timeout")

    cdp = setup_cdp(monkeypatch, fail=fail)
    with pytest.raises(browser.BrowserSetupError) as caught:
        browser.Browser("https://example.test/")
    assert caught.value.phase == "navigate" and caught.value.cause_type == "TimeoutError"
    assert sum(c.args[0] == "Page.navigate" for c in cdp.call_args_list) == 1
