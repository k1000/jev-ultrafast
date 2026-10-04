"""Frame facts are read-only, modal-scoped, opt-in evidence; offline with a fake CDP layer."""

import pytest

from jev_ultrafast.browser import Browser


def snapshot(n=2, w=400, h=300, labels=("Accept", "Reject")):
    controls = [{"role": "button", "label": label} for label in labels][:n]
    return {"result": {"value": {"w": w, "h": h, "modal_open": False, "n": n, "controls": controls}}}


class FakeCdp:
    def __init__(self, targets=(), tree_children=(), snaps=None, fail=None):
        self.calls, self.snaps, self.fail = [], snaps or {}, fail
        self.targets, self.tree_children = list(targets), list(tree_children)

    def __call__(self, method, **params):
        self.calls.append((method, params))
        if self.fail == method:
            raise RuntimeError("boom")
        if method == "Target.getTargets":
            return {"targetInfos": self.targets}
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "top"},
                                  "childFrames": [{"frame": f} for f in self.tree_children]}}
        if method == "Target.attachToTarget":
            return {"sessionId": "S-" + params["targetId"]}
        if method == "Page.createIsolatedWorld":
            return {"executionContextId": "W-" + params["frameId"]}
        if method == "Runtime.evaluate":
            key = params.get("_session") or params.get("contextId")
            return self.snaps[key]
        return {}


def browser(fake):
    b = Browser.__new__(Browser)
    b.call = lambda method, **params: fake(method, **params)
    return b


MODAL = {"modal_open": True}
OOPIF = {"targetId": "t1", "type": "iframe", "url": "https://cmp.example/consent?session_handoff=secret"}


def test_no_modal_means_no_frame_reads():
    fake = FakeCdp(targets=[OOPIF])
    assert browser(fake).frame_facts({"modal_open": False}) == []
    assert fake.calls == []


def test_out_of_process_frame_reads_counts_labels_and_only_the_host():
    fake = FakeCdp(targets=[OOPIF], snaps={"S-t1": snapshot()})
    rows = browser(fake).frame_facts(MODAL)
    assert rows == [{"kind": "out_of_process", "host": "cmp.example", "controls": 2,
                     "labels": [{"role": "button", "label": "Accept"}, {"role": "button", "label": "Reject"}],
                     "modal_open": False}]
    assert "secret" not in repr(rows)
    assert ("Target.detachFromTarget", {"sessionId": "S-t1"}) in fake.calls


def test_in_process_cross_origin_frame_uses_an_isolated_world():
    fake = FakeCdp(tree_children=[{"id": "f2", "url": "https://c.site.test/x?a=1"}], snaps={"W-f2": snapshot(1)})
    rows = browser(fake).frame_facts(MODAL)
    assert rows[0]["kind"] == "in_process" and rows[0]["host"] == "c.site.test" and rows[0]["controls"] == 1
    assert not any(m == "Target.attachToTarget" for m, _ in fake.calls)


def test_separate_target_is_not_read_twice_through_the_frame_tree():
    fake = FakeCdp(targets=[OOPIF], tree_children=[{"id": "t1", "url": OOPIF["url"]}], snaps={"S-t1": snapshot()})
    assert len(browser(fake).frame_facts(MODAL)) == 1


def test_tiny_tracker_frames_are_dropped():
    fake = FakeCdp(targets=[OOPIF], snaps={"S-t1": snapshot(0, w=1, h=1)})
    assert browser(fake).frame_facts(MODAL) == []


def test_unreadable_frame_is_an_error_row_not_an_exception():
    fake = FakeCdp(targets=[OOPIF], snaps={"S-t1": {"exceptionDetails": {}}})
    assert browser(fake).frame_facts(MODAL)[0]["error"] == "unreadable"
    broken = FakeCdp(targets=[OOPIF], fail="Target.attachToTarget")
    assert browser(broken).frame_facts(MODAL)[0]["error"] == "RuntimeError"
    assert browser(FakeCdp(fail="Page.getFrameTree")).frame_facts(MODAL) == [{"error": "RuntimeError"}]


def test_frames_are_capped_and_never_issue_input():
    targets = [{**OOPIF, "targetId": f"t{i}"} for i in range(9)]
    fake = FakeCdp(targets=targets, snaps={f"S-t{i}": snapshot() for i in range(9)})
    assert len(browser(fake).frame_facts(MODAL)) == 4
    assert not any(m.startswith("Input.") for m, _ in fake.calls)


def test_feature_is_off_by_default():
    assert Browser.frame_facts_enabled is False


@pytest.mark.parametrize("label_len", [200])
def test_labels_are_truncated_in_the_page_script(label_len):
    from jev_ultrafast.browser import FRAME_FACTS
    assert "label.slice(0,80)" in FRAME_FACTS and "slice(0,12)" in FRAME_FACTS
