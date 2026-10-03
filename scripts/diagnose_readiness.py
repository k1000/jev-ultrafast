"""One owned Flights bootstrap, retained on failure; same-target read-only transport comparison.

No models, search inputs, browser restarts, debugger resume, or navigation retries.
"""

import argparse
import json
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen

from browser_harness.helpers import cdp
from websockets.sync.client import connect

from jev_ultrafast.browser import READ_STATE, Browser, BrowserSetupError

CDP = "http://127.0.0.1:9333"
URL = "https://www.google.com/travel/flights?hl=en"


def summarize(value):
    if isinstance(value, dict) and "actions" in value:
        return {"ready": value.get("ready_state"), "actions": len(value["actions"]),
                "controls": len(value.get("controls", [])), "modal_open": value.get("modal_open")}
    if isinstance(value, dict) and "url" in value:
        url = urlsplit(value["url"])
        return {"host": url.hostname, "path": url.path, "ready": value.get("ready")}
    return value if isinstance(value, (int, bool, type(None))) else {"type": type(value).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--target", help="Read the previously retained owned target; do not navigate")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    if os.environ.get("BU_CDP_URL") != CDP:
        parser.error("Require isolated Comet on 9333")
    pid = subprocess.check_output(["lsof", "-nP", "-t", "-iTCP:9333", "-sTCP:LISTEN"], text=True).splitlines()[0]
    command = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True)
    if "/Comet.app/" not in command or "Comet-JevAutomation" not in command:
        parser.error("Port must belong to isolated Comet")
    with urlopen(CDP + "/json/version", timeout=2) as response:
        endpoint = json.load(response)["webSocketDebuggerUrl"]
    with urlopen(CDP + "/json/list", timeout=2) as response:
        direct_targets = {t["id"] for t in json.load(response) if t.get("type") == "page"}
    harness_targets = {t["targetId"] for t in cdp("Target.getTargets").get("targetInfos", [])
                       if t.get("type") == "page"}
    if direct_targets != harness_targets:
        parser.error("Harness must target the same isolated Comet before creating any tab")
    if args.target and args.target not in direct_targets:
        parser.error("Retained target is not in isolated Comet")
    report = {"scope": __doc__, "paid_calls": 0, "search_inputs": 0, "probes": [], "events": [],
              "direct_calls": []}
    browser = Browser.__new__(Browser)
    close = browser.close
    browser.close = lambda: None  # Diagnostic only: do not lose the failed target during constructor cleanup.
    try:
        try:
            if args.target:
                browser.target = args.target
                browser.session = browser.call("Target.attachToTarget", targetId=args.target, flatten=True)["sessionId"]
                report["bootstrap"] = "not_replayed; inspect retained target only"
            else:
                Browser.__init__(browser, URL)
                report["bootstrap"] = "ready"
        except BrowserSetupError as exc:
            report["bootstrap"] = {"phase": exc.phase, "cause_type": exc.cause_type}
        finally:
            browser.close = close
        report["retained_target"] = browser.target
        if not browser.target or not hasattr(browser, "session"):
            raise RuntimeError("No owned attached target to inspect")
        # This websocket has its own session on precisely the same owned page, not a new navigation.
        with connect(endpoint, origin=CDP, open_timeout=3, close_timeout=1) as ws:
            request_id = 0

            def direct(method, params=None, session=None):
                nonlocal request_id
                request_id += 1
                issued = request_id
                message = {"id": issued, "method": method, "params": params or {}}
                if session:
                    message["sessionId"] = session
                call = {"method": method, "status": "issued"}
                report["direct_calls"].append(call)
                ws.send(json.dumps(message))
                deadline = time.monotonic() + 3
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Direct read deadline")
                    reply = json.loads(ws.recv(timeout=remaining))
                    if "method" in reply:
                        event = {"method": reply["method"]}
                        if reply["method"] == "Target.attachedToTarget":
                            event["waitingForDebugger"] = reply.get("params", {}).get("waitingForDebugger")
                        report["events"].append(event)
                    if reply.get("id") == issued:
                        if "error" in reply:
                            call["status"] = "rejected"
                            raise RuntimeError("Direct CDP read rejected")
                        call["status"] = "returned"
                        return reply.get("result", {})

            session = direct("Target.attachToTarget", {"targetId": browser.target, "flatten": True})["sessionId"]
            try:
                tree = direct("Page.getFrameTree", session=session)
                frame = tree.get("frameTree", {}).get("frame", {})
                url = urlsplit(frame.get("url", ""))
                report["frame"] = {"host": url.hostname, "path": url.path, "has_loader": bool(frame.get("loaderId"))}
            except Exception as exc:
                report["frame_error"] = type(exc).__name__
            # Non-evaluating DOM read helps distinguish JS execution from target/renderer connectivity.
            try:
                dom = direct("DOM.getDocument", {"depth": 0}, session)
                report["dom_document_returned"] = bool(dom.get("root"))
            except Exception as exc:
                report["dom_error"] = type(exc).__name__

            def probe(route, expression):
                started = time.perf_counter()
                result = {"route": route}
                try:
                    reply = (browser.call("Runtime.evaluate", expression=expression, returnByValue=True,
                                          _response_timeout=3, _phase="diagnostic_read") if route == "harness" else
                             direct("Runtime.evaluate", {"expression": expression, "returnByValue": True}, session))
                    if reply.get("exceptionDetails"):
                        result["js_exception"] = True
                    else:
                        result["value"] = summarize(reply.get("result", {}).get("value"))
                except Exception as exc:
                    result["error"] = type(exc).__name__
                result["ms"] = round((time.perf_counter() - started) * 1000, 3)
                return result

            with ThreadPoolExecutor(max_workers=2) as pool:
                for label, expression in (("simple", "1+1"),
                                          ("readiness", "({url:location.href,ready:document.readyState})"),
                                          ("snapshot", READ_STATE)):
                    tasks = [pool.submit(probe, route, expression) for route in ("harness", "direct_cdp")]
                    for task in tasks:
                        result = {"check": label, **task.result()}
                        report["probes"].append(result)
                        print(json.dumps(result), flush=True)
    except Exception as exc:
        report["error"] = type(exc).__name__
    finally:
        # Leave even a recovered diagnostic target open for inspection; no input or navigation replay.
        for call in report["direct_calls"]:
            if call["status"] == "issued":
                call["status"] = "no_reply_within_read_budget"
        report["browser_calls"] = getattr(browser, "cdp_calls", [])
        report["retained_target"] = getattr(browser, "target", None)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps({k: report.get(k) for k in ("bootstrap", "frame", "dom_document_returned",
                                                   "dom_error", "error", "retained_target")}), flush=True)


if __name__ == "__main__":
    main()
