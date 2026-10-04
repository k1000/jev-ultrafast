"""Offline DOM fixture for native-dialog precedence and hit-tested overlay modals."""

import json
import subprocess
from pathlib import Path

import pytest

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"

JS = r"""
const vm=require('node:vm'), fs=require('node:fs');
const fixture=JSON.parse(process.argv[2]);
const rect=(x,y,width,height)=>({x,y,width,height});
const overlayRect=fixture.placement==='header'?rect(0,0,800,80):
  fixture.placement==='banner'?rect(0,530,800,70):rect(0,0,800,490);
const input={tagName:'INPUT',type:'text',value:'',checked:false,selectedIndex:-1,disabled:false,
  readOnly:false,isConnected:true,labels:[],parentElement:null,getAttribute:k=>k==='aria-label'?'Search':null,
  closest:()=>null,matches:()=>false,checkVisibility:()=>true,contains:()=>false,
  getBoundingClientRect:()=>rect(20,100,100,30)};
const heading={textContent:fixture.heading||'Overlay heading'};
const overlay={tagName:'DIV',type:'',isConnected:true,parentElement:null,
  getAttribute:k=>k==='aria-label'?fixture.label||null:k==='role'?null:null,
  closest:()=>null,matches:()=>false,checkVisibility:()=>true,contains:e=>e===overlayButton,
  querySelector:()=>heading,getBoundingClientRect:()=>overlayRect};
const overlayButton={tagName:'BUTTON',type:'button',value:'Dismiss',isConnected:true,labels:[],parentElement:overlay,
  getAttribute:k=>k==='aria-label'?'Dismiss':null,
  closest:()=>null,matches:()=>false,checkVisibility:()=>true,contains:()=>false,
  getBoundingClientRect:()=>rect(300,60,90,30)};
const dialog={tagName:'DIALOG',type:'',isConnected:true,parentElement:null,
  getAttribute:k=>k==='aria-modal'?null:k==='aria-label'?'Native dialog':null,
  closest:()=>null,matches:()=>false,checkVisibility:()=>true,contains:e=>e===dialogButton,
  querySelector:()=>heading,getBoundingClientRect:()=>rect(0,0,800,490)};
const dialogButton={...overlayButton,parentElement:dialog,getAttribute:k=>k==='aria-label'?'Native close':null};
const inRect=(r,x,y)=>x>=r.x&&x<r.x+r.width&&y>=r.y&&y<r.y+r.height;
const hit=(x,y)=>fixture.native&&inRect(dialog.getBoundingClientRect(),x,y)?dialogButton:
  fixture.position!=='static'&&inRect(overlayRect,x,y)?overlayButton:input;
const document={body:{},title:'Fixture',readyState:'complete',documentElement:{scrollHeight:600},
  getElementById:()=>null,
  querySelectorAll:q=>q==='input,textarea,select'?[input]:q==='*'?[overlay,overlayButton,input,dialog,dialogButton]:
    q.includes('dialog[open]')?fixture.native?[dialog]:[]:
    fixture.native?[input,overlayButton,dialogButton]:[input,overlayButton],
  elementFromPoint:hit,createTreeWalker:()=>({nextNode:()=>null}),
  createRange:()=>({selectNodeContents:()=>{},getBoundingClientRect:()=>rect(0,0,0,0)})};
const context={document,window:{},getComputedStyle:e=>({position:e===overlay?fixture.position:'static'}),
  location:{href:'https://example.test/',origin:'https://example.test',pathname:'/'},
  performance:{timeOrigin:123},scrollX:0,scrollY:0,innerWidth:800,innerHeight:600,
  NodeFilter:{SHOW_TEXT:4}};
vm.createContext(context);
const source=fs.readFileSync(process.argv[1],'utf8');
const first=vm.runInContext(source,context);
fixture.heading='Changed heading';heading.textContent=fixture.heading;
const second=vm.runInContext(source,context);
console.log(JSON.stringify([first,second]));
"""


def snapshot(**fixture):
    result = subprocess.run(["node", "-e", JS, str(SNAPSHOT), json.dumps(fixture)],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_overlay_obscures_background_and_exposes_only_its_actions_and_heading():
    first, changed = snapshot(placement="large", position="fixed", heading="Sign in to continue")
    assert first["modal_open"] is True and first["modal_label"] == "Sign in to continue"
    assert next(c for c in first["controls"] if c["label"] == "Search")["observable"] is False
    assert not any(a["label"] == "Search" for a in first["actions"])
    assert any(a["label"] == "Dismiss" for a in first["actions"])
    assert first["semantic_marker"] != changed["semantic_marker"]
    assert first["terminal_marker"] != changed["terminal_marker"]


@pytest.mark.parametrize("fixture", [
    {"placement": "header", "position": "sticky"},
    {"placement": "banner", "position": "fixed"},
    {"placement": "large", "position": "static"},
])
def test_small_or_nonfixed_regions_are_not_promoted_to_modal(fixture):
    first, _ = snapshot(**fixture)
    assert first["modal_open"] is False and first["modal_label"] == ""
    assert next(c for c in first["controls"] if c["label"] == "Search")["observable"] is True


def test_role_dialog_wins_precedence_even_with_overlay_and_accessible_label():
    first, _ = snapshot(placement="large", position="fixed", native=True, label="Overlay",
                        heading="Ignored heading")
    assert first["modal_open"] is True and first["modal_label"] == "Native dialog"
    assert next(c for c in first["controls"] if c["label"] == "Search")["observable"] is False


def test_overlay_aria_label_has_priority_and_is_bounded():
    first, _ = snapshot(placement="large", position="sticky", label="Secret " + "x" * 100)
    assert first["modal_open"] is True and first["modal_label"] == ("Secret " + "x" * 73)
