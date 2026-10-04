"""Offline clock/churn regressions for target identity and semantic progress."""

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

from jev_ultrafast.browser import Browser, StalePage
from jev_ultrafast.objective import ObjectiveAgent

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"


def clock_snapshots():
    script = r"""
const vm=require('node:vm'), fs=require('node:fs');
let tick='Clock 1', secondInput=null;
const form={tagName:'FORM',innerText:'Search '+tick,getAttribute:()=>null,querySelector:()=>null};
const input={type:'text',tagName:'INPUT',value:'',checked:false,selectedIndex:-1,
  disabled:false,readOnly:false,isConnected:true,labels:[],parentElement:form,
  getAttribute:k=>k==='aria-label'?'Search':null,
  closest:q=>q.includes('fieldset')?form: q.includes('form')?form:null,
  matches:()=>false,checkVisibility:()=>true,
  getBoundingClientRect:()=>({x:10,y:10,width:100,height:20})};
const parent={closest:()=>null,checkVisibility:()=>true};
const textNode={nodeType:3,parentElement:parent,get textContent(){return tick}};
const document={body:{},title:'Example',readyState:'complete',documentElement:{scrollHeight:700},
  getElementById:()=>null,
  querySelectorAll:q=>q==='input,textarea,select'?(secondInput?[input,secondInput]:[input]):
    q.includes('dialog')?[]:secondInput?[input,secondInput]:[input],
  elementFromPoint:(x)=>x>=200&&secondInput?secondInput:input,
  createTreeWalker:()=>{let n=0;return {nextNode:()=>n++?null:textNode}},
  createRange:()=>({selectNodeContents:()=>{},getBoundingClientRect:()=>({x:0,y:0,width:100,height:20,bottom:20,top:0,right:100,left:0})})};
const context={document,window:{},getComputedStyle:()=>({position:'static'}),
  location:{href:'https://example.test/?tick=1',origin:'https://example.test',pathname:'/'},
  performance:{timeOrigin:123},scrollX:0,scrollY:0,innerWidth:800,innerHeight:600,
  NodeFilter:{SHOW_TEXT:4}};
vm.createContext(context);
const source=fs.readFileSync(process.argv[1],'utf8');
const first=vm.runInContext(source,context);
tick='Clock 2';form.innerText='Search '+tick;
const second=vm.runInContext(source,context);
context.location.href='https://example.test/?tick=2';
const third=vm.runInContext(source,context);
secondInput={...input,labels:[],getBoundingClientRect:()=>({x:230,y:10,width:100,height:20})};
const duplicate=vm.runInContext(source,context);
secondInput.type='search';
const crossRole=vm.runInContext(source,context);
const otherForm={...form,getAttribute:k=>k==='aria-label'?'Alternative':null};
secondInput.parentElement=otherForm;
secondInput.closest=q=>q.includes('fieldset')?otherForm:q.includes('form')?otherForm:null;
const crossGroup=vm.runInContext(source,context);
console.log(JSON.stringify([first,second,third,duplicate,crossRole,crossGroup]));
"""
    result = subprocess.run(["node", "-e", script, str(SNAPSHOT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_clock_changes_debug_and_terminal_but_not_semantic_or_fill_identity():
    first, second, query_changed, _, _, _ = clock_snapshots()
    assert first["text"] != second["text"]
    assert first["marker"] != second["marker"]
    assert first["terminal_marker"] != second["terminal_marker"]
    assert first["semantic_marker"] == second["semantic_marker"]
    fill = next(a for a in first["actions"] if a["kind"] == "fill")
    key = f'{fill["node"]}:fill'
    assert first["identity_marker"][key] == second["identity_marker"][key]
    assert first["semantic_marker"] == query_changed["semantic_marker"]
    assert first["identity_marker"][key] != query_changed["identity_marker"][key]
    browser = Browser.__new__(Browser)
    browser.evaluate = Mock(return_value=second["identity_marker"][key])
    assert browser.fresh(first, fill)
    browser.evaluate.return_value = query_changed["identity_marker"][key]
    assert not browser.fresh(first, fill)


def test_clock_churn_does_not_authorize_replaying_identical_click():
    first, second, _, _, _, _ = clock_snapshots()
    click = next(a for a in first["actions"] if a["kind"] == "click")
    controller = ObjectiveAgent.__new__(ObjectiveAgent)
    controller.agent = Mock(state={"decision": {"choice": click["id"]}, "page": second})
    controller.last_mutation = (("click", click["node"], click["value"]),
                                first["semantic_marker"], second["semantic_marker"])
    assert controller.repeated_mutation()
    changed = deepcopy(second)
    changed["semantic_marker"] = ["new control facts"]
    controller.agent.state["page"] = changed
    assert not controller.repeated_mutation()


def test_duplicate_recipient_after_chooser_rejects_fill_before_any_browser_mutation():
    _, _, query_changed, duplicate, _, _ = clock_snapshots()
    selected = next(a for a in query_changed["actions"] if a["kind"] == "fill")
    key = f'{selected["node"]}:fill'
    assert selected["node"] == next(a for a in duplicate["actions"] if a["kind"] == "fill")["node"]
    assert query_changed["guards"][str(selected["node"])] == duplicate["guards"][str(selected["node"])]
    assert query_changed["url"] == duplicate["url"] and not query_changed["modal_open"]
    assert query_changed["identity_marker"][key] != duplicate["identity_marker"][key]

    browser = Browser.__new__(Browser)
    browser.evaluate = Mock(return_value=duplicate["identity_marker"][key])
    browser.call = Mock(side_effect=AssertionError("Mutation transport must never be called"))
    assert not browser.fresh(query_changed, selected)
    receipt = browser.execute(selected, query_changed, text="prepared value")
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    assert receipt["phase"] == "freshness" and receipt["error"] == "StalePage"
    browser.call.assert_not_called()
    try:
        browser.act(selected, query_changed, text="prepared value")
        assert False, "Expected StalePage, not a policy rejection"
    except StalePage as exc:
        assert exc.receipt["status"] == "rejected_before_input"
    browser.call.assert_not_called()


def test_cross_role_or_group_same_caption_peer_rejects_selected_fill():
    _, _, selected_page, _, cross_role, cross_group = clock_snapshots()
    selected = next(a for a in selected_page["actions"] if a["kind"] == "fill")
    key = f'{selected["node"]}:fill'
    for changed in (cross_role, cross_group):
        assert selected_page["guards"][str(selected["node"])] == changed["guards"][str(selected["node"])]
        assert selected_page["identity_marker"][key] != changed["identity_marker"][key]
        browser = Browser.__new__(Browser)
        browser.evaluate = Mock(return_value=changed["identity_marker"][key])
        browser.call = Mock(side_effect=AssertionError("No mutation transport"))
        assert not browser.fresh(selected_page, selected)
        receipt = browser.execute(selected, selected_page, text="prepared value")
        assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
        browser.call.assert_not_called()
    assert {c["role"] for c in cross_role["controls"]} == {"textbox", "searchbox"}
    assert len({c.get("group") for c in cross_group["controls"]}) == 2


def test_capped_control_swap_cannot_authorize_any_fill_even_when_omission_count_is_unchanged():
    script = r"""
const vm=require('node:vm'),fs=require('node:fs');
const inputs=Array.from({length:251},(_,i)=>({
  tagName:'INPUT',type:'text',label:i===0?'Search':i===250?'Other':'Field '+i,
  value:'',checked:false,selectedIndex:-1,disabled:false,readOnly:false,
  isConnected:true,labels:[],parentElement:null,
  getAttribute(k){return k==='aria-label'?this.label:null},closest:()=>null,
  matches:()=>false,checkVisibility:()=>true,
  getBoundingClientRect:()=>({x:10+i%20*30,y:10+Math.floor(i/20)*30,width:20,height:20})
}));
const document={body:{},title:'Cap',readyState:'complete',documentElement:{scrollHeight:600},
  getElementById:()=>null,
  querySelectorAll:q=>q.includes('dialog')?[]:inputs,
  elementFromPoint:(x,y)=>inputs.find(e=>{const r=e.getBoundingClientRect();
    return x>=r.x&&x<r.x+r.width&&y>=r.y&&y<r.y+r.height}),
  createTreeWalker:()=>({nextNode:()=>null}),createRange:()=>({})};
const context={document,window:{},getComputedStyle:()=>({position:'static'}),
  location:{href:'https://example.test/',origin:'https://example.test',pathname:'/'},
  performance:{timeOrigin:123},scrollX:0,scrollY:0,innerWidth:800,innerHeight:600,
  NodeFilter:{SHOW_TEXT:4}};
vm.createContext(context);const source=fs.readFileSync(process.argv[1],'utf8');
const first=vm.runInContext(source,context);
inputs[250].label='Search';
const swapped=vm.runInContext(source,context);
console.log(JSON.stringify([first,swapped]));
"""
    result = subprocess.run(["node", "-e", script, str(SNAPSHOT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    first, swapped = json.loads(result.stdout)
    selected = next(a for a in first["actions"] if a["kind"] == "fill")
    key = f'{selected["node"]}:fill'
    assert first["omitted_controls"] == swapped["omitted_controls"] == 1
    assert first["guards"][str(selected["node"])] == swapped["guards"][str(selected["node"])]
    assert first["identity_marker"][key] is swapped["identity_marker"][key] is None
    browser = Browser.__new__(Browser)
    browser.evaluate = Mock(return_value=swapped["identity_marker"][key])
    browser.call = Mock(side_effect=AssertionError("No mutation transport"))
    assert not browser.fresh(first, selected)
    receipt = browser.execute(selected, first, text="prepared value")
    assert receipt["status"] == "rejected_before_input" and receipt["input_started"] is False
    browser.call.assert_not_called()


def test_stale_settles_with_churning_text_without_replaying(monkeypatch):
    first, second, _, _, _, _ = clock_snapshots()
    controller = ObjectiveAgent.__new__(ObjectiveAgent)
    controller.status = "ready"
    controller.events = []
    controller.limit_stop = lambda: False
    controller.observe = Mock(side_effect=[deepcopy(second), deepcopy(second)])
    monkeypatch.setattr("jev_ultrafast.objective.time.sleep", lambda _: None)
    rejected = deepcopy(first)
    rejected["semantic_marker"] = ["old controls"]
    assert controller.settle_after_stale(rejected)
    assert controller.observe.call_count == 2
    assert controller.feedback["reason"] == "page_settled"
