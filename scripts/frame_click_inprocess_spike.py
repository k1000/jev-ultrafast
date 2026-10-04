"""Owned-fixture spike: does a page-session click at composed coordinates reach a same-site (in-process) frame?

Usage: BU_CDP_URL=http://127.0.0.1:9340 uv run python scripts/frame_click_inprocess_spike.py URL --output NEW.json
Dispatches exactly one click (mousePressed + mouseReleased) on an OWNED fixture; never run on a public site.
Refuses to overwrite evidence. No model calls.
"""

import argparse
import json
import time
from pathlib import Path

from browser_harness.helpers import cdp

from jev_ultrafast.browser import Browser

TARGET = ("(() => { const b=document.querySelector('button'); const r=b.getBoundingClientRect();"
          " return {label:b.textContent,x:r.x+r.width/2,y:r.y+r.height/2,clicks:b.dataset.clicks||'0'}; })()")
CLICKS = "document.querySelector('button').dataset.clicks || '0'"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output exists; evidence is never overwritten")
    browser = Browser(args.url)
    page, report = browser.session, {}
    try:
        time.sleep(3)
        tree = cdp("Page.getFrameTree", session_id=page)["frameTree"]
        child = tree["childFrames"][0]["frame"]["id"]
        report["frame_is_separate_target"] = any(
            t["targetId"] == child for t in cdp("Target.getTargets")["targetInfos"])
        world = cdp("Page.createIsolatedWorld", frameId=child, worldName="spike", session_id=page)["executionContextId"]

        def read(expression):
            return cdp("Runtime.evaluate", expression=expression, returnByValue=True, contextId=world,
                       session_id=page)["result"]["value"]

        local = read(TARGET)
        cdp("DOM.enable", session_id=page)
        cdp("DOM.getDocument", session_id=page)
        owner = cdp("DOM.getFrameOwner", frameId=child, session_id=page)["backendNodeId"]
        quad = cdp("DOM.getBoxModel", backendNodeId=owner, session_id=page)["model"]["content"]
        px, py = quad[0] + local["x"], quad[1] + local["y"]
        hit = cdp("DOM.getNodeForLocation", x=int(px), y=int(py), session_id=page)
        report.update(label=local["label"], local_point=[round(local["x"]), round(local["y"])],
                      page_point=[round(px), round(py)], hit_frame_is_child=hit.get("frameId") == child,
                      clicks_before=read(CLICKS))
        for event in ("mousePressed", "mouseReleased"):
            cdp("Input.dispatchMouseEvent", type=event, x=px, y=py, button="left", clickCount=1, session_id=page)
        time.sleep(0.3)
        report["clicks_after"] = read(CLICKS)
    finally:
        browser.close()
    with args.output.open("x") as file:
        json.dump(report, file, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
