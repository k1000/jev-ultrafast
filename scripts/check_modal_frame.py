"""Owned local cross-origin modal click fixture; no model, public site, or retry."""

import argparse
import hashlib
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.request import urlopen

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

from jev_ultrafast import browser as browser_module
from scripts.consent_probe import ConsentOnlyBrowser, preflight
from scripts.live_safety import safe_reject


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        port = self.server.server_port
        if self.path == "/parent":
            body = f'''<!doctype html><title>Local consent fixture</title>
            <div role="dialog" aria-label="Local cookie consent">
              <iframe title="Local choices" src="http://localhost:{port}/child"
                      style="width:400px;height:250px;border:0"></iframe></div>
            <script>
              addEventListener('message', e => {{
                if (e.origin !== 'http://localhost:{port}' ||
                    e.source !== document.querySelector('iframe')?.contentWindow ||
                    e.data?.kind !== 'local-reject' || e.data?.clicks !== '1') return;
                document.body.dataset.rejected='1';
                document.querySelector('[role="dialog"]').remove();
              }});
            </script>'''
        elif self.path == "/child":
            body = f'''<!doctype html><title>Local choices</title>
            <button aria-label="Reject" onclick="
              document.body.dataset.clicks=String(Number(document.body.dataset.clicks||0)+1);
              parent.postMessage({{kind:'local-reject',clicks:document.body.dataset.clicks}},
                'http://127.0.0.1:{port}')">Reject</button>
            <button aria-label="Accept all">Accept all</button>'''
        else:
            self.send_error(404)
            return
        payload = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *_args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Refusing to overwrite evidence")
    if Path(browser_module.__file__).resolve() != Path(__file__).resolve().parents[1] / "jev_ultrafast/browser.py":
        parser.error("Require the local worktree package")
    if (os.environ.get("BU_NAME") != "jev-chrome-diagnostic" or
            os.environ.get("BU_CDP_URL") != "http://127.0.0.1:9334"):
        parser.error("Require isolated diagnostic Chrome on port 9334")
    pid = subprocess.check_output(["lsof", "-nP", "-t", "-iTCP:9334", "-sTCP:LISTEN"],
                                  text=True).splitlines()[0]
    process = subprocess.check_output(["ps", "-p", pid, "-o", "command="], text=True)
    if "/Google Chrome.app/" not in process or "Chrome-JevDiagnostics" not in process:
        parser.error("Require isolated Chrome profile")
    ensure_daemon(wait=15)
    with urlopen("http://127.0.0.1:9334/json/list", timeout=3) as response:
        direct = {t["id"] for t in json.load(response) if t.get("type") == "page"}
    if direct != {t["targetId"] for t in cdp("Target.getTargets")["targetInfos"] if t.get("type") == "page"}:
        parser.error("Harness target set must match isolated Chrome")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    blocked = Mock(side_effect=AssertionError("Model/helper requests prohibited"))
    report = {"scope": "Owned local cross-origin modal fixture; no public-site input",
              "source_hashes": {path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
                                for path in ("jev_ultrafast/browser.py", "jev_ultrafast/snapshot.js", __file__)}}
    browser = None
    phase = "owned_target_observation"
    input_started = False
    try:
        with patch("jev_ultrafast.model.post_json", blocked), \
                patch("jev_ultrafast.planning.post_json", blocked), \
                patch("jev_ultrafast.agent.choose", blocked), \
                patch("jev_ultrafast.agent.field_texts", blocked):
            url = f"http://127.0.0.1:{server.server_port}/parent"
            phase = "read_only_preflight"
            assert preflight(url, "Local consent fixture") == url
            phase = "owned_target_observation"
            browser = ConsentOnlyBrowser(url)
            page = browser.observe(screenshot=False)
            candidates = [a for a in page["actions"] if a.get("frame_id") and
                          a.get("kind") == "click" and a.get("role") == "button" and
                          safe_reject(a.get("label"))]
            assert len(candidates) == 1 and page["modal_open"] is True
            assert page["unindexed_modal_frames"] == 0
            assert not any(a.get("label") == "Accept all" and safe_reject(a.get("label"))
                           for a in page["actions"])
            phase = "single_local_click"
            receipt = browser.execute(candidates[0], page)
            input_started = receipt["input_started"]
            report.update(receipt_status=receipt["status"], input_started=input_started,
                          receipt_phase=receipt["phase"],
                          phases=[c["phase"] for c in receipt["calls"]])
            phase = "independent_outcome"
            observed = None
            for _ in range(10):
                observed = browser.observe(screenshot=False)
                if observed.get("modal_open") is False:
                    break
                time.sleep(0.1)  # Read only; never resend input.
            marker = browser.evaluate("document.body.dataset.rejected || null")
            report["checks"] = {"unique_frame_preflight": True,
                                "single_local_input": receipt["status"] == "executed" and input_started,
                                "modal_closed": observed["modal_open"] is False,
                                "rejection_marker": marker == "1",
                                "same_url": observed["url"] == page["url"],
                                "no_model_calls": not blocked.called}
            report["passed"] = all(report["checks"].values())
            assert report["passed"]
            browser.close()
    except (Exception, KeyboardInterrupt, SystemExit) as exc:
        report.update(passed=False, error=type(exc).__name__, failure_phase=phase)
        if browser is not None and not input_started:
            browser.close()
    finally:
        report["model_or_helper_calls"] = blocked.call_count
        if browser is not None and browser.target:
            report["retained_target"] = browser.target  # Inspect only; never retry unknown input.
        server.shutdown()
        server.server_close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as evidence:
            json.dump(report, evidence, indent=2)
            evidence.write("\n")
        print(json.dumps({k: report.get(k) for k in ("passed", "failure_phase", "input_started",
                                                    "model_or_helper_calls", "retained_target")}))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
