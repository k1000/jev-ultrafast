"""Owned-fixture end-to-end: Browser clicks a button inside a same-site (in-process) modal iframe.

Usage: BU_CDP_URL=http://127.0.0.1:9340 uv run python scripts/check_inprocess_frame_click.py URL --output NEW.json
Runs three owned-fixture cases through Browser.observe/execute with frame_clicks_enabled:
  click      -> exactly one click lands in the frame
  moved      -> frame moves after observation: rejected before input, zero clicks
  navigated  -> frame navigates after observation: rejected before input
Never run on a public site. No model calls. Refuses to overwrite evidence.
"""

import argparse
import json
import time
from pathlib import Path

from browser_harness.helpers import cdp

from jev_ultrafast.browser import Browser

CLICKS = "document.querySelector('button').dataset.clicks || '0'"


def frame_clicks(browser):
    tree = cdp("Page.getFrameTree", session_id=browser.session)["frameTree"]["childFrames"][0]["frame"]["id"]
    world = cdp("Page.createIsolatedWorld", frameId=tree, worldName="check", session_id=browser.session)
    return cdp("Runtime.evaluate", expression=CLICKS, returnByValue=True, contextId=world["executionContextId"],
               session_id=browser.session)["result"]["value"]


def run(url, case):
    browser = Browser(url)
    browser.frame_clicks_enabled = True
    try:
        time.sleep(3)
        page = browser.observe(screenshot=False)
        actions = [a for a in page["actions"] if a.get("frame_id")]
        out = {"case": case, "framed_actions": len(actions), "unindexed_after": page.get("unindexed_modal_frames")}
        if len(actions) != 1:
            return out
        if case == "moved":
            browser.evaluate("document.querySelector('iframe').style.marginLeft='37px'")
        elif case == "navigated":
            browser.evaluate("document.querySelector('iframe').src='http://c.a.test:8801/ss_click.html?x=1'")
            time.sleep(1)
        receipt = browser.execute(actions[0], page)
        out.update(status=receipt["status"], phase=receipt["phase"], input_started=receipt["input_started"],
                   clicks_in_frame=frame_clicks(browser))
        return out
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("--output exists; evidence is never overwritten")
    results = [run(args.url, case) for case in ("click", "moved", "navigated")]
    with args.output.open("x") as file:
        json.dump(results, file, indent=2)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
