"""Offline frame boundary: inaccessible modal content is evidence, never an action."""

import json
import subprocess
from pathlib import Path

import pytest
from test_agent import choice

from jev_ultrafast import model

SNAPSHOT = Path(__file__).resolve().parents[1] / "jev_ultrafast" / "snapshot.js"

JS = r"""
const vm=require('node:vm'),fs=require('node:fs');
const mode=process.argv[2],rect=(x,y,width,height)=>({x,y,width,height});
const bounds=mode==='offscreen'?rect(900,20,300,240):rect(60,40,300,240);
const frame={tagName:'IFRAME',isConnected:true,parentElement:null,
  getAttribute:k=>k==='src'?'https://cmp.test/?session_handoff=private-token':null,
  closest:()=>null,checkVisibility:()=>mode!=='hidden',
  getBoundingClientRect:()=>bounds};
const modal={tagName:'DIV',isConnected:true,parentElement:null,
  getAttribute:k=>k==='role'?'dialog':k==='aria-label'?'SP Consent Message':null,
  closest:()=>null,matches:()=>false,checkVisibility:()=>true,
  contains:e=>e===frame,querySelectorAll:q=>q==='iframe,frame'&&mode!=='outside'?[frame]:[],
  getBoundingClientRect:()=>rect(0,0,800,500)};
const input={tagName:'INPUT',type:'text',value:'',checked:false,selectedIndex:-1,
  disabled:false,readOnly:false,isConnected:true,labels:[],parentElement:null,
  getAttribute:k=>k==='aria-label'?'Background search':null,
  closest:()=>null,matches:()=>false,checkVisibility:()=>true,
  getBoundingClientRect:()=>rect(20,20,90,20)};
const hit=(x,y)=>x>=bounds.x&&x<bounds.x+bounds.width&&y>=bounds.y&&y<bounds.y+bounds.height?
  mode==='covered'?{tagName:'DIV'}:frame:modal;
const document={body:{},title:'Fixture',readyState:'complete',documentElement:{scrollHeight:600},
  getElementById:()=>null,querySelectorAll:q=>q==='input,textarea,select'?[input]:
    q.includes('dialog[open]')?[modal]:[input],
  elementFromPoint:hit,createTreeWalker:()=>({nextNode:()=>null}),createRange:()=>({})};
const context={document,window:{},getComputedStyle:()=>({position:'static'}),
  location:{href:'https://example.test/',origin:'https://example.test',pathname:'/'},
  performance:{timeOrigin:123},scrollX:0,scrollY:0,innerWidth:800,innerHeight:600,
  NodeFilter:{SHOW_TEXT:4}};
vm.createContext(context);
console.log(JSON.stringify(vm.runInContext(fs.readFileSync(process.argv[1],'utf8'),context)));
"""


def snapshot(mode):
    result = subprocess.run(["node", "-e", JS, str(SNAPSHOT), mode],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("mode,expected", [
    ("cross-origin", 1), ("same-origin", 1), ("covered", 0),
    ("offscreen", 0), ("hidden", 0), ("outside", 0),
])
def test_only_visible_modal_owned_frame_is_flagged_without_exposing_actions_or_url(mode, expected):
    page = snapshot(mode)
    assert page["modal_open"] and page["modal_label"] == "SP Consent Message"
    assert page["unindexed_modal_frames"] == expected
    assert not any(a.get("role") == "iframe" for a in page["actions"])
    assert not any(a.get("label") == "Background search" for a in page["actions"])
    assert "private-token" not in json.dumps(page)


def test_frame_occlusion_invalidates_terminal_and_semantic_evidence():
    observed, covered = snapshot("cross-origin"), snapshot("covered")
    assert observed["semantic_marker"] != covered["semantic_marker"]
    assert observed["terminal_marker"] != covered["terminal_marker"]


def test_chooser_sees_unsupported_frame_count_not_url_or_executable_target(monkeypatch):
    page = snapshot("cross-origin")

    def post(_url, _key, body):
        assert body["state"]["page"]["unindexed_modal_frames"] == 1
        assert "iframe" not in str(body["state"]["elements"])
        assert "private-token" not in json.dumps(body)
        return {"model": "offline", "answers": {
            "operation": choice(body["questions"]["operation"]["criteria"], "BLOCKED"),
        }}

    monkeypatch.setenv("TYPESAFE_API_KEY", "offline-only")
    monkeypatch.setattr(model, "post_json", post)
    model.choose(page, "Decline optional cookies", [])
