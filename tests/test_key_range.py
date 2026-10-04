"""Offline contracts for closed observed keyboard/range operations; no providers."""

import json
import subprocess
import time
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest

from jev_ultrafast import browser as browser_module
from jev_ultrafast import model
from jev_ultrafast.agent import Agent
from jev_ultrafast.browser import PolicyRejected, fingerprint
from jev_ultrafast.objective import ObjectiveAgent
from jev_ultrafast.planning import Check, Text

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"


def test_snapshot_offers_only_focused_editable_enter_modal_escape_and_observed_range():
    source = r"""
const fs=require('node:fs'), vm=require('node:vm');let opened=false,submitVisible=false,inside=false;
const rect=(x,y,width,height)=>({x,y,width,height});
const text={tagName:'INPUT',type:'search',value:'prepared query',checked:false,selectedIndex:-1,
  disabled:false,readOnly:false,isConnected:true,labels:[],parentElement:null,
  getAttribute:k=>k==='aria-label'?'Search':null,closest:()=>null,matches:()=>false,
  checkVisibility:()=>true,contains:()=>false,getBoundingClientRect:()=>rect(20,30,100,30)};
const slider={...text,type:'range',value:'0.1',min:'0.1',max:'0.9',step:'0.2',
  getAttribute:k=>k==='aria-label'?'Budget':null,getBoundingClientRect:()=>rect(240,30,100,30)};
const dialog={tagName:'DIALOG',isConnected:true,parentElement:null,
  getAttribute:k=>k==='aria-label'?'Filter dialog':null,closest:()=>null,matches:()=>false,
  checkVisibility:()=>true,contains:e=>e===dismiss||(inside&&e===text),querySelector:()=>null,
  getBoundingClientRect:()=>rect(0,0,800,600)};
const dismiss={tagName:'BUTTON',type:'button',value:'Dismiss',isConnected:true,labels:[],parentElement:dialog,
  getAttribute:k=>k==='aria-label'?'Dismiss':null,closest:()=>null,matches:()=>false,
  checkVisibility:()=>true,contains:()=>false,getBoundingClientRect:()=>rect(300,40,90,30)};
const submit={tagName:'BUTTON',type:'submit',value:'Submit',labels:[],childNodes:[],
  isConnected:true,parentElement:null,
  getAttribute:()=>null,closest:()=>null,matches:()=>false,checkVisibility:()=>true,
  contains:()=>false,getBoundingClientRect:()=>rect(400,30,100,30)};
const document={body:{},title:'Fixture',readyState:'complete',activeElement:text,
  documentElement:{scrollHeight:600},getElementById:()=>null,
  querySelectorAll:q=>q==='input,textarea,select'?[text,slider]:
    q.includes('dialog[open]')?opened?[dialog]:[]:
    q.includes('button[type="submit"]')?submitVisible?[submit]:[]:
    opened?[text,slider,dismiss]:submitVisible?[text,slider,submit]:[text,slider],
  elementFromPoint:(x)=>opened?(inside&&x<200?text:dismiss):
    x<200?text:submitVisible&&x>=400?submit:slider,
  createTreeWalker:()=>({nextNode:()=>null}),createRange:()=>({})};
const context={document,window:{},getComputedStyle:()=>({position:'static'}),
  location:{href:'https://example.test/',origin:'https://example.test',pathname:'/'},
  performance:{timeOrigin:1},scrollX:0,scrollY:0,innerWidth:800,innerHeight:600,
  NodeFilter:{SHOW_TEXT:4}};
vm.createContext(context);const code=fs.readFileSync(process.argv[1],'utf8');
const first=vm.runInContext(code,context);
opened=true;document.activeElement=dismiss;
const second=vm.runInContext(code,context);
document.activeElement=text; // Background focus must never authorize a modal key.
const backgroundFocused=vm.runInContext(code,context);
inside=true;
const modalFocusedField=vm.runInContext(code,context);
opened=false;slider.min='';slider.max='';slider.step=''; // Explicit bounds required for this small v1.
const defaultBounds=vm.runInContext(code,context);
slider.min='0.1';slider.max='0.9';slider.step='0.2';submitVisible=true;
const exposedSubmit=vm.runInContext(code,context);
const associatedForm={getAttribute:()=>null,querySelector:()=>null};
text.closest=q=>q==='form'?associatedForm:null;
submit.form=associatedForm;submit.closest=()=>null; // External form= submit control.
const externalSubmit=vm.runInContext(code,context);
console.log(JSON.stringify([first,second,backgroundFocused,modalFocusedField,
  defaultBounds,exposedSubmit,externalSubmit]));
"""
    result = subprocess.run(["node", "-e", source, str(SNAPSHOT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    (focused, modal, background_focused, modal_focused, default_bounds,
     exposed_submit, external_submit) = json.loads(result.stdout)
    search_node = next(a["node"] for a in focused["actions"] if a.get("label") == "Search")
    assert [(a["key"], a["node"]) for a in focused["actions"] if a["kind"] == "press_key"] == [
        ("Escape", search_node), ("Enter", search_node)]
    range_actions = [a for a in focused["actions"] if a["kind"] == "set_range"]
    assert len(range_actions) == 1
    assert {k: range_actions[0][k] for k in ("min", "max", "step", "value")} == {
        "min": "0.1", "max": "0.9", "step": "0.2", "value": "0.1"}
    assert not any(a["kind"] == "set_range" for a in modal["actions"])
    assert [(a["key"], a["role"], a["label"]) for a in modal["actions"]
            if a["kind"] == "press_key"] == [("Escape", "dialog", "Filter dialog")]
    assert not any(a["kind"] == "press_key" for a in background_focused["actions"])
    assert [(a["key"], a["role"]) for a in modal_focused["actions"]
            if a["kind"] == "press_key"] == [("Escape", "dialog")]
    assert not any(a["kind"] == "set_range" for a in default_bounds["actions"])
    assert [a["key"] for a in exposed_submit["actions"] if a["kind"] == "press_key"] == ["Escape"]
    assert [a["key"] for a in external_submit["actions"] if a["kind"] == "press_key"] == ["Escape"]


def key_action(key="Enter"):
    return {"id": "e1", "kind": "press_key", "node": 5, "key": key,
            "label": "Search", "role": "searchbox", "value": "query"}


def range_action():
    return {"id": "e2", "kind": "set_range", "node": 6, "label": "Budget",
            "role": "slider", "min": "0.1", "max": "0.9", "step": "0.2", "value": "0.1"}


def client(monkeypatch, replies):
    browser = browser_module.Browser.__new__(browser_module.Browser)
    browser.session = "owned-session"
    browser.fresh = Mock(return_value=True)
    cdp = Mock(side_effect=replies)
    monkeypatch.setattr(browser_module, "cdp", cdp)
    return browser, cdp


def test_action_heads_disambiguate_closed_keys_on_same_node_and_only_selected_head(monkeypatch):
    keys = [key_action("Escape"), {**key_action("Enter"), "id": "e3"}]
    elements, targets, _ = model.action_space([*keys, range_action()])[:3]
    assert len(elements) == 2
    assert set(targets["PRESS_KEY"]) == {"1:Escape", "1:Enter"}
    assert set(targets["SET_RANGE"]) == {"2"}

    def post(_url, _key, body):
        heads = body["questions"]
        operation = heads["operation"]["criteria"]
        assert "PRESS_KEY" in operation and "SET_RANGE" in operation
        return {"model": "stub", "answers": {
            "operation": {"choice": "PRESS_KEY", "confidence": 1.,
                          "probabilities": {key: float(key == "PRESS_KEY") for key in operation}},
            "press_key_target": {"choice": "1:Enter", "confidence": 1.,
                                 "probabilities": {key: float(key == "1:Enter")
                                                   for key in heads["press_key_target"]["criteria"]}},
            "set_range_target": {"choice": "invented", "confidence": -1},
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    chosen = model.choose({"url": "https://example.test", "title": "Example", "text": "",
                           "actions": [*keys, range_action()]}, "Submit the search", [])
    assert chosen["choice"] == "e3" and chosen["operation"] == "PRESS_KEY"


@pytest.mark.parametrize("key", ["Tab", "Control+Enter", "escape", "", None])
def test_unknown_or_unobserved_keys_never_send_input(monkeypatch, key):
    browser, cdp = client(monkeypatch, [])
    action = key_action(key)
    receipt = browser.execute(action, {"actions": [action]})
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    cdp.assert_not_called()


def test_key_down_and_up_are_each_marked_input_before_send(monkeypatch):
    browser, cdp = client(monkeypatch, [{"result": {"value": {"authorized": True}}}, {}, {}])
    action = key_action()
    receipt = browser.execute(action, {"actions": [action]})
    assert receipt["status"] == "executed" and receipt["input_started"] is True
    assert [c["phase"] for c in receipt["calls"]] == ["key_target", "keyDown", "keyUp"]
    assert [c["method"] for c in receipt["calls"]] == ["Runtime.evaluate", "Input.dispatchKeyEvent",
                                                       "Input.dispatchKeyEvent"]
    assert [c["status"] for c in receipt["calls"]] == ["returned", "returned", "returned"]
    assert [call.kwargs["type"] for call in cdp.call_args_list[1:]] == ["keyDown", "keyUp"]
    assert cdp.call_args_list[1].kwargs["text"] == "\r"
    assert cdp.call_args_list[1].kwargs["unmodifiedText"] == "\r"
    assert "text" not in cdp.call_args_list[2].kwargs


def test_escape_key_never_inserts_text(monkeypatch):
    browser, cdp = client(monkeypatch, [{"result": {"value": {"authorized": True}}}, {}, {}])
    action = key_action("Escape")
    receipt = browser.execute(action, {"actions": [action]})
    assert receipt["status"] == "executed"
    assert all("text" not in call.kwargs for call in cdp.call_args_list[1:])


def test_unknown_key_ack_locks_further_input_and_no_key_up_or_retry(monkeypatch):
    browser, cdp = client(monkeypatch, [{"result": {"value": {"authorized": True}}}, TimeoutError()])
    action = key_action()
    receipt = browser.execute(action, {"actions": [action]})
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"] is True
    assert receipt["phase"] == "keyDown" and cdp.call_count == 2
    assert browser.execute(action, {"actions": [action]}) == receipt
    cdp.assert_called()
    assert cdp.call_count == 2


def test_unknown_key_up_ack_is_locked_after_key_down_not_retried(monkeypatch):
    browser, cdp = client(monkeypatch, [{"result": {"value": {"authorized": True}}}, {}, TimeoutError()])
    action = key_action()
    receipt = browser.execute(action, {"actions": [action]})
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"] is True
    assert [c["phase"] for c in receipt["calls"]] == ["key_target", "keyDown", "keyUp"]
    assert [c["status"] for c in receipt["calls"]] == ["returned", "returned", "error"]
    assert receipt["phase"] == "keyUp" and cdp.call_count == 3
    assert browser.execute(action, {"actions": [action]}) == receipt
    assert cdp.call_count == 3


def test_enter_caller_guard_denies_before_key_down(monkeypatch):
    from scripts.objective_flights import SearchOnlyBrowser

    browser = SearchOnlyBrowser.__new__(SearchOnlyBrowser)
    browser.session = "owned-session"
    browser.fresh = Mock(return_value=True)
    cdp = Mock()
    monkeypatch.setattr(browser_module, "cdp", cdp)
    action = {**key_action(), "label": "Press Enter", "target_label": "Sign in", "form_label": "Sign in"}
    receipt = browser.execute(action, {"actions": [action]})
    assert receipt["status"] == "rejected_by_policy" and receipt["input_started"] is False
    cdp.assert_not_called()


def test_agent_path_retains_caller_denial_without_dispatching_enter(monkeypatch):
    from scripts.objective_flights import SearchOnlyBrowser

    browser = SearchOnlyBrowser.__new__(SearchOnlyBrowser)
    browser.session = "owned-session"
    browser.fresh = Mock(return_value=True)
    cdp = Mock()
    monkeypatch.setattr(browser_module, "cdp", cdp)
    action = {**key_action(), "label": "Press Enter", "target_label": "Sign in", "form_label": "Sign in"}
    page = {"actions": [action], "fingerprint": "fresh", "url": "https://example.test"}
    runner = Agent.__new__(Agent)
    runner.state = {"browser": browser, "page": page, "decision": {
        "choice": "e1", "operation": "PRESS_KEY", "target": "1:Enter", "probabilities": {"e1": 1.},
        "confidence": 1., "latency_ms": 0, "usage": {}},
        "history": [], "attempts": [], "started_at": time.perf_counter(), "status": "predicted"}
    with pytest.raises(PolicyRejected):
        runner.command("act", {"fingerprint": "fresh"})
    assert runner.state["attempts"][-1]["status"] == "rejected_by_policy"
    assert runner.state["history"] == [] and runner.state["status"] == "ready"
    cdp.assert_not_called()


@pytest.mark.parametrize("text", ["0.3", "0.9"])
def test_range_uses_supplied_decimal_on_exact_grid_and_one_marked_mutation(monkeypatch, text):
    browser, cdp = client(monkeypatch, [{"result": {"value": {"authorized": True}}},
                                           {"result": {"value": {"set": True}}}])
    action = range_action()
    receipt = browser.execute(action, {"actions": [action]}, text=text)
    assert receipt["status"] == "executed" and receipt["input_started"] is True
    assert [c["phase"] for c in receipt["calls"]] == ["range_bounds", "set_range"]
    assert all(c["method"] == "Runtime.evaluate" for c in receipt["calls"])
    assert cdp.call_count == 2
    assert "dispatchEvent" in cdp.call_args_list[1].kwargs["expression"]
    assert Text("Budget", text, role="slider").value == text  # Caller-supplied immutable plan value.


@pytest.mark.parametrize("text", ["0.4", "0.0", "1.1", "nan", "Infinity", "", "1e10000", "free"])
def test_range_rejects_offgrid_out_of_bounds_or_nonfinite_value_preinput(monkeypatch, text):
    browser, cdp = client(monkeypatch, [])
    action = range_action()
    receipt = browser.execute(action, {"actions": [action]}, text=text)
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    cdp.assert_not_called()


def test_range_rejects_non_range_or_stale_bounds_before_mutation(monkeypatch):
    browser, cdp = client(monkeypatch, [{"result": {"value": {"rejected": True}}}])
    action = range_action()
    receipt = browser.execute(action, {"actions": [action]}, text="0.3")
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    assert cdp.call_count == 1


class RangeBrowser:
    def __init__(self, url):
        action = range_action()
        self.page = {"url": url, "title": "Budget", "text": "Budget", "scroll": {"y": 0},
                     "document_id": 1, "modal_open": False, "actions": [action],
                     "controls": [{"node": 6, "kind": "control", "role": "slider", "label": "Budget",
                                   "value": "0.1", "observable": True, "disabled": False, "readonly": False}]}
        self.page["fingerprint"] = fingerprint(self.page)
        self.calls = []

    def observe(self, screenshot=False):
        return deepcopy(self.page)

    def fresh(self, page, action=None, *, terminal=False):
        return page["fingerprint"] == self.page["fingerprint"]

    def act(self, action, page, text=None):
        self.calls.append((action["id"], text))
        self.page["actions"][0]["value"] = text
        self.page["controls"][0]["value"] = text
        self.page["fingerprint"] = fingerprint(self.page)
        return {"status": "executed"}

    def close(self):
        pass


def range_plan(texts):
    goal = "Set Budget to 0.3"
    return {"objective": goal, "plan": [{"goal": goal, "texts": texts,
            "checks": [{"kind": "value", "label": "Budget", "role": "slider", "value": "0.3"}]}]}


def test_controller_uses_only_unique_current_step_prepared_slider_text(monkeypatch):
    from jev_ultrafast import agent as loop

    monkeypatch.setattr(loop, "Browser", RangeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value={
        "choice": "e2", "operation": "SET_RANGE", "target": "1", "confidence": 1.,
        "probabilities": {"e2": 1.}, "latency_ms": 0, "usage": {}}))
    helper = Mock(side_effect=AssertionError("No text helper allowed"))
    planner = Mock(side_effect=AssertionError("No model planner allowed"))
    monkeypatch.setattr(loop, "field_texts", helper)
    with ObjectiveAgent("https://example.test/", "Set Budget to 0.3",
                        checks=(Check("value", "0.3", label="Budget", role="slider"),),
                        prepared_plan=range_plan([{"label": "Budget", "role": "slider", "value": "0.3"}]),
                        planner=planner) as controller:
        state = controller.command()
        assert state["status"] == "done" and state["verification"] == [True]
        assert controller.browser.calls == [("e2", "0.3")]
        assert state["text_calls"] == [] and state["planner_calls"] == []
    helper.assert_not_called()
    planner.assert_not_called()


@pytest.mark.parametrize("texts", [[], [
    {"label": "Budget", "role": "slider", "value": "0.3"},
    {"label": "Budget", "value": "0.5"},
]])
def test_controller_rejects_missing_or_conflicting_slider_prepared_text_before_input(monkeypatch, texts):
    from jev_ultrafast import agent as loop

    monkeypatch.setattr(loop, "Browser", RangeBrowser)
    monkeypatch.setattr(loop, "choose", Mock(return_value={
        "choice": "e2", "operation": "SET_RANGE", "target": "1", "confidence": 1.,
        "probabilities": {"e2": 1.}, "latency_ms": 0, "usage": {}}))
    helper = Mock(side_effect=AssertionError("No text helper allowed"))
    planner = Mock(side_effect=AssertionError("No model planner allowed"))
    monkeypatch.setattr(loop, "field_texts", helper)
    with ObjectiveAgent("https://example.test/", "Set Budget to 0.3",
                        checks=(Check("value", "0.3", label="Budget", role="slider"),),
                        prepared_plan=range_plan(texts), planner=planner, local_attempts=0) as controller:
        state = controller.command()
        assert state["status"] != "done" and state["verification"] == [False]
        assert controller.browser.calls == [] and state["text_calls"] == []
    helper.assert_not_called()
    planner.assert_not_called()


@pytest.mark.parametrize("kind", ["press_key", "set_range"])
def test_controller_rejects_second_unchanged_key_or_range_without_second_input(monkeypatch, kind):
    from jev_ultrafast import agent as loop

    class NoEffectBrowser(RangeBrowser):
        def __init__(self, url):
            super().__init__(url)
            if kind == "press_key":
                self.page["actions"] = [key_action()]
                self.page["fingerprint"] = fingerprint(self.page)

        def act(self, action, page, text=None):
            self.calls.append((action["id"], text))
            return {"status": "executed"}  # Acknowledged but no observable change.

    monkeypatch.setattr(loop, "Browser", NoEffectBrowser)
    target = "e1" if kind == "press_key" else "e2"
    choice = Mock(return_value={"choice": target, "operation": "PRESS_KEY" if kind == "press_key" else "SET_RANGE",
                                "target": "1", "confidence": 1., "probabilities": {target: 1.},
                                "latency_ms": 0, "usage": {}})
    monkeypatch.setattr(loop, "choose", choice)
    checks = (Check("text", "Search submitted"),) if kind == "press_key" else (
        Check("value", "0.3", label="Budget", role="slider"),)
    plan = {"objective": "Set Budget to 0.3", "plan": []} if kind == "press_key" else range_plan([
        {"label": "Budget", "role": "slider", "value": "0.3"}])
    with ObjectiveAgent("https://example.test/", "Set Budget to 0.3", checks=checks,
                        prepared_plan=plan, local_attempts=0, settle_timeout=0) as controller:
        first = controller.command()
        assert first["status"] == "ready" and len(controller.browser.calls) == 1
        assert controller.last_mutation[0] == (("press_key", 5, "query", "Enter") if kind == "press_key"
                                               else ("set_range", 6, "0.3"))
        second = controller.command()
        assert second["status"] != "done" and len(controller.browser.calls) == 1
        assert len(second["decisions"]) == 2 and choice.call_count == 2


@pytest.mark.parametrize("mode", ["last_mutation", "forbidden_target"])
def test_press_key_exclusions_distinguish_escape_from_enter_on_same_node(monkeypatch, mode):
    from test_agent import choice as answer

    actions = [key_action("Escape"), {**key_action("Enter"), "id": "e3"}]
    marker = ["stable"]
    page = {"url": "https://example.test", "title": "Search", "text": "", "actions": actions,
            "semantic_marker": marker}
    last = (("press_key", 5, "query", "Enter"), marker, marker)

    def post(_url, _key, body):
        offered = body["questions"]["press_key_target"]["criteria"]
        assert set(offered) == {"1:Escape"}
        return {"model": "stub", "answers": {
            "operation": answer(body["questions"]["operation"]["criteria"], "PRESS_KEY"),
            "press_key_target": answer(offered, "1:Escape"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "test")
    monkeypatch.setattr(model, "post_json", post)
    execution = ({"_last_mutation": last} if mode == "last_mutation" else
                 {"_forbidden_targets": ((repr(marker), "press_key", 5, "query", "Enter"),)})
    chosen = model.choose(page, "Dismiss the autocomplete", [], execution=execution)
    assert chosen["choice"] == "e1"


def test_range_unknown_mutation_ack_locks_execution_without_replay(monkeypatch):
    browser, cdp = client(monkeypatch, [{"result": {"value": {"authorized": True}}}, TimeoutError()])
    action = range_action()
    receipt = browser.execute(action, {"actions": [action]}, text="0.3")
    assert receipt["status"] == "outcome_unknown" and receipt["input_started"] is True
    assert receipt["phase"] == "set_range" and cdp.call_count == 2
    assert browser.execute(action, {"actions": [action]}, text="0.3") == receipt
    assert cdp.call_count == 2
