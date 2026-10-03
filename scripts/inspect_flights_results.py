"""Reopen one recorded Flights results URL once and inspect, with no models or input actions."""

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlsplit
from urllib.request import urlopen

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from jev_ultrafast import browser as module
from jev_ultrafast import model, planning
from scripts.objective_flights import FACTS, SearchOnlyBrowser, verify_flights

PROBE = """(() => {
  const labels=[...document.querySelectorAll('[aria-label]')];
  const describe=e=>{const r=e.getBoundingClientRect();return {
    tag:e.tagName,role:e.getAttribute('role'),label:e.getAttribute('aria-label'),
    visible:e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}),
    rect:{top:r.top,bottom:r.bottom,width:r.width,height:r.height},
    in_viewport:r.bottom>0&&r.top<innerHeight&&r.width>0,
    text:e.innerText?.slice(0,400),display:getComputedStyle(e).display};};
  const exact=labels.filter(e=>e.getAttribute('aria-label').startsWith('Select flight'));
  const candidates=labels.filter(e=>/flight/i.test(e.getAttribute('aria-label')));
  const text=document.body.innerText;
  const busy=[...document.querySelectorAll('[aria-busy="true"],[role="progressbar"]')]
    .filter(e=>e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
  const state=new RegExp('no flights|couldn.t find|not available|try again|loading|departing flights|'+
    'unusual traffic|something went wrong','i');
  return {document_ready:document.readyState,viewport:{width:innerWidth,height:innerHeight},
    exact_rows:exact.slice(0,30).map(describe),exact_row_count:exact.length,
    flight_label_candidates:candidates.slice(0,30).map(describe),flight_label_count:candidates.length,
    busy_count:busy.length,body_text:text.slice(0,6500),
    state_lines:text.split('\\n').filter(s=>state.test(s)).slice(0,20)};
})()"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("docs/objective-flights-chrome-attempt.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    source = json.loads(args.source.read_text())
    url = source.get("final_url", "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "www.google.com" or parsed.path != "/travel/flights/search":
        parser.error("Source must contain a recorded public Flights results URL")
    if (os.environ.get("BU_NAME") != "jev-chrome-diagnostic" or
            os.environ.get("BU_CDP_URL") != "http://127.0.0.1:9334"):
        parser.error("Require separate Chrome diagnostic Harness on port 9334")
    pid = subprocess.check_output(["lsof", "-nP", "-t", "-iTCP:9334", "-sTCP:LISTEN"], text=True).splitlines()[0]
    command = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True)
    if "/Google Chrome.app/" not in command or "Chrome-JevDiagnostics" not in command:
        parser.error("Require isolated stock Chrome profile")
    ensure_daemon(wait=15)
    with urlopen("http://127.0.0.1:9334/json/list", timeout=3) as response:
        direct = {t["id"] for t in json.load(response) if t.get("type") == "page"}
    if direct != {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}:
        parser.error("Harness must target the same isolated Chrome")
    report = {"scope": __doc__, "source_evidence": str(args.source), "samples": [],
              "source_hashes": {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in
                                ("jev_ultrafast/browser.py", "jev_ultrafast/snapshot.js",
                                 "scripts/objective_flights.py", __file__)}}
    reader, model_post, planner_post = module.READ_STATE, model.post_json, planning.post_json
    blocked = Mock(side_effect=AssertionError("No model or input API invocation permitted"))
    model.post_json = planning.post_json = blocked
    module.READ_STATE = ("(() => {const page=" + reader + ";if(!page)return null;page.flight_facts=" + FACTS +
                         ";page.result_probe=" + PROBE + ";return page;})()")
    browser = None
    try:
        browser = SearchOnlyBrowser(url)  # Only navigation, never a search-button replay.
        browser.execute = browser.act = blocked
        started = time.monotonic()
        for offset in (0, 1, 3, 6, 10, 20, 30):
            time.sleep(max(0, started + offset - time.monotonic()))
            try:
                page = browser.observe(screenshot=False, response_timeout=3)
                current = urlsplit(page["url"])
                sample = {"offset_seconds": offset, "elapsed_ms": round((time.monotonic() - started) * 1000),
                          "host": current.hostname, "path": current.path,
                          "verification": verify_flights(page), "probe": page["result_probe"],
                          "observed_flight_actions": [{k: a.get(k) for k in ("id", "kind", "label", "position")}
                                                      for a in page["actions"] if "flight" in a["label"].lower()]}
                report["samples"].append(sample)
                fields = ("exact_row_count", "flight_label_count", "busy_count", "state_lines")
                print(json.dumps({"seconds": offset, "checks": sample["verification"]["checks"],
                                  **{k: sample["probe"][k] for k in fields}}), flush=True)
                if current.hostname != "www.google.com" or current.path != "/travel/flights/search":
                    report["error"] = "RecordedResultsUnavailable"
                    break
            except Exception as exc:
                report["samples"].append({"offset_seconds": offset, "error": type(exc).__name__})
            args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report["error"] = type(exc).__name__
        if isinstance(exc, module.BrowserSetupError):
            report.update(retained_target=exc.retained_target,
                          setup_error={"phase": exc.phase, "cause_type": exc.cause_type,
                                       "cleanup_error": exc.cleanup_error})
    finally:
        module.READ_STATE, model.post_json, planning.post_json = reader, model_post, planner_post
        report.update(model_or_input_calls=blocked.call_count,
                      retained_target=browser.target if browser else report.get("retained_target"),
                      browser_calls=getattr(browser, "cdp_calls", []))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        print(json.dumps({"evidence": str(args.output), "retained_target": report["retained_target"],
                          "model_or_input_calls": blocked.call_count, "error": report.get("error")}), flush=True)
    return 1 if report.get("error") else 0


if __name__ == "__main__":
    raise SystemExit(main())
