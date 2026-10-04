"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import math
import sys
import time
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from pathlib import Path
from uuid import uuid4

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
TERMINAL_MARKER = f"(() => {{ const state={READ_STATE}; return state?.terminal_marker ?? null; }})()"

def _range_number(value):
    if type(value) is not str or not value or len(value) > 100:
        raise ValueError("Range needs a bounded numeric string")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError("Range needs a finite number") from None
    if not number.is_finite():
        raise ValueError("Range needs a finite number")
    return number


def _validate_range_value(action, value):
    if action.get("role") != "slider":
        raise ValueError("Range must be an observed native slider")
    lower, upper, step = (_range_number(action.get(key)) for key in ("min", "max", "step"))
    target = _range_number(value)
    if step <= 0 or upper < lower or not lower <= target <= upper:
        raise ValueError("Range value is outside observed bounds")
    try:
        if (target - lower) % step != 0:
            raise ValueError("Range value does not lie on the observed step grid")
    except InvalidOperation:
        raise ValueError("Invalid range step grid") from None


def _positive_seconds(value):
    if type(value) not in {int, float} or not math.isfinite(value) or value <= 0:
        raise ValueError("Supply positive finite seconds")
    return value


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


class PolicyRejected(ValueError):
    """Caller policy denied an observed action before browser input."""


class _TargetRejected(StalePage):
    """Code-owned target validation returned explicitly before any mutation."""


class ExecutionUncertain(RuntimeError):
    """Input may have occurred. The receipt is evidence, not permission to retry."""

    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__(f"Browser execution uncertain during {receipt['phase']} ({receipt.get('error')})")


class BrowserSetupError(RuntimeError):
    """Safe bootstrap diagnostics; no page content, credential data, or raw server errors."""

    def __init__(self, phase, cause):
        self.phase, self.cause_type = phase, type(cause).__name__
        self.cleanup_error = self.retained_target = None
        super().__init__(f"Browser setup failed during {phase} ({self.cause_type})")


class Browser:
    def __init__(self, url):
        self.target = None
        phase = "daemon"
        try:
            ensure_daemon()
            phase = "create_target"
            self.target = self.call("Target.createTarget", url="about:blank", background=True)["targetId"]
            phase = "attach"
            self.session = self.call("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            phase = "emulation"
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
            # Keep owned background tabs rendering without activating the user's tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            phase = "navigate"
            navigation = self.call("Page.navigate", url=url, _response_timeout=15)
            if navigation.get("errorText"):
                raise RuntimeError("Initial navigation was rejected")
            phase = "document_ready"
            deadline = time.monotonic() + 15
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    state = self.evaluate("({url:location.href,ready:document.readyState})",
                                          response_timeout=min(5, remaining))
                except (StalePage, TimeoutError):
                    state = None  # Only retry this read; navigation itself is never replayed.
                if (isinstance(state, dict) and state.get("ready") == "complete"
                        and (url == "about:blank" or state.get("url") not in {None, "about:blank"})):
                    return
                time.sleep(0.02)
            raise TimeoutError("Initial document did not finish loading")
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            error = BrowserSetupError(phase, exc)
            try:
                self.close()
            except Exception as cleanup:
                error.cleanup_error = type(cleanup).__name__
                error.retained_target = self.target
            if isinstance(exc, Exception):
                raise error from exc
            raise

    def call(self, method, *, _phase=None, _input=False, **params):
        if not hasattr(self, "cdp_calls"):
            self.cdp_calls = []
        receipt = getattr(self, "_execution", None)
        phase = _phase or (receipt["phase"] if receipt else method)
        event = {"method": method, "phase": phase, "status": "issued"}
        self.cdp_calls.append(event)
        if receipt is not None:
            receipt["calls"].append(event)
            receipt["phase"] = phase
            if _input:
                receipt["input_started"] = True  # Before sending, not after receiving an acknowledgment.
        began = time.perf_counter()
        try:
            result = cdp(method, session_id=None if method.startswith("Target.") else self.session, **params)
            if (not isinstance(result, dict) or "error" in result
                    or (_input and method.startswith("Input.") and result != {})):
                raise RuntimeError("Invalid CDP acknowledgment")
            event["status"] = "returned"
            return result
        except (Exception, KeyboardInterrupt, SystemExit) as exc:
            event.update(status="error", error=type(exc).__name__)
            raise
        finally:
            event["ms"] = round((time.perf_counter() - began) * 1000, 3)

    def evaluate(self, expression, *, response_timeout=None):
        params = {} if response_timeout is None else {"_response_timeout": response_timeout}
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True, **params)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    def observe(self, screenshot=True, *, response_timeout=None):
        deadline = None if response_timeout is None else time.monotonic() + _positive_seconds(response_timeout)

        def read_call(method, **params):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Observation read deadline exhausted")
                params["_response_timeout"] = min(remaining, params.get("_response_timeout", 5))
            return self.call(method, **params)

        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                read_call(
                    "Runtime.evaluate", _phase="post_input_wait",
                    expression="""(action => new Promise(resolve => {
                      const field=window.__jevFast?.nodes.get(action.node);
                      const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
                      let frames=0, stopped=false;
                      const finish=()=>{stopped=true;resolve()};
                      setTimeout(finish,autocomplete ? 200 : 50);
                      const ready=()=>{
                        if (stopped) return;
                        const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
                          .split(/\\s+/).filter(Boolean);
                        const roots=ids.length ? ids.map(id=>document.getElementById(id)).filter(Boolean) : [document];
                        const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
                        if (++frames>=2 && (!autocomplete || options.some(e=>{
                          const r=e.getBoundingClientRect();
                          return r.width && r.height && r.bottom>0 && r.top<innerHeight &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
                        }))) finish();
                        else requestAnimationFrame(ready);
                      };
                      requestAnimationFrame(ready);
                    }))(""" + json.dumps(action) + ")",
                    awaitPromise=True,
                    returnByValue=True,
                )
            except RuntimeError:
                pass
        for attempt in range(10):
            try:
                return browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot},
                    transport=read_call,
                )
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None, *, terminal=False):
        if terminal:
            if action is not None:
                raise ValueError("Terminal freshness cannot authorize a browser action.")
            return self.evaluate(TERMINAL_MARKER) == page["terminal_marker"]
        if action is not None and action["kind"] in {"click", "fill", "select", "scroll_to",
                                                     "press_key", "set_range"}:
            node = action["node"]
            if type(node) is not int:
                return False
            if "identity_marker" in page:
                identity = json.dumps(f"{node}:{action['kind']}")
                current = self.evaluate(f"(() => {{ const state={READ_STATE}; "
                                        f"return state?.identity_marker?.[{identity}] ?? null; }})()")
                expected = page["identity_marker"].get(f"{node}:{action['kind']}")
                return expected is not None and current == expected
            # Compatibility for synthetic observations that predate the per-node marker.
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        return self.evaluate(MARKER) == page["marker"]

    def validate_action(self, action, page, text=None):
        """Pre-input validation shared by execute/act; subclasses may add safety policy."""
        if (action.get("kind") not in {"click", "fill", "select", "scroll_to", "scroll", "wait",
                                       "press_key", "set_range"}
                or sum(a == action for a in page["actions"]) != 1
                or sum(a.get("id") == action.get("id") for a in page["actions"]) != 1):
            raise ValueError("Action must match a unique observed action")
        if action["kind"] == "fill":
            if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                raise ValueError("Invalid prepared text")
        elif action["kind"] == "set_range":
            _validate_range_value(action, text)
        elif text is not None:
            raise ValueError("Text is only supported for fill or set_range")
        if action["kind"] == "press_key" and (action.get("key") not in {"Escape", "Enter"} or
                                                type(action.get("node")) is not int):
            raise ValueError("Only observed Escape and Enter targets are supported")

    def execute(self, action, page, text=None):
        """Single-shot observed action; unknown input locks execution but permits reads."""
        if getattr(self, "_uncertain_receipt", None) is not None:
            return deepcopy(self._uncertain_receipt)
        if not hasattr(self, "receipts"):
            self.receipts = []
        receipt = {"request_id": uuid4().hex, "action_id": action.get("id"),
                   "status": "issued", "phase": "validation", "input_started": False, "calls": []}
        self.receipts.append(receipt)  # Record intent before guards or possible input.
        self._execution = receipt
        began = time.perf_counter()
        try:
            if getattr(self, "_closed", False):
                raise ValueError("Browser is closed")
            self.validate_action(action, page, text)
            receipt["phase"] = "freshness"
            if not self.fresh(page, action):
                raise StalePage("Page changed since this decision. Observe again.")
            if action["kind"] == "wait":
                time.sleep(0.1)
            browser_operation({"operation": "act", "session": self.session, "action": action, "text": text},
                              transport=self.call)
            self.after_input = action if action["kind"] != "wait" else None
            receipt["status"] = "executed"
        except PolicyRejected as exc:
            receipt.update(status="rejected_by_policy" if not receipt["input_started"] and
                           receipt["phase"] == "validation" else "outcome_unknown",
                           error=type(exc).__name__)
        except Exception as exc:
            if isinstance(exc, _TargetRejected):
                receipt["input_started"] = False
            receipt.update(status="outcome_unknown" if receipt["input_started"] else "rejected_before_input",
                           error=type(exc).__name__)
        except (KeyboardInterrupt, SystemExit) as exc:
            receipt.update(status="outcome_unknown" if receipt["input_started"] else "rejected_before_input",
                           error=type(exc).__name__)
            raise
        finally:
            receipt["elapsed_ms"] = round((time.perf_counter() - began) * 1000, 3)
            self._execution = None
            if receipt["status"] == "outcome_unknown":
                self._uncertain_receipt = receipt
        return deepcopy(receipt)

    def settle(self, checks, *, timeout=5, interval=0.1, verifier=None):
        """Poll fresh caller criteria; never execute, replay, or unlock an uncertain action.

        verifier is optional trusted, read-only server code returning a strict boolean.
        A timeout is not proof of impossibility. No model or helper is invoked.
        """
        from .planning import Check, evidence

        checks = tuple(checks)
        if not checks or any(not isinstance(c, Check) for c in checks):
            raise ValueError("Supply nonempty caller-owned outcome checks")
        timeout, interval = _positive_seconds(timeout), _positive_seconds(interval)
        if verifier is not None and not callable(verifier):
            raise ValueError("Verifier must be trusted read-only callable server code")
        started = time.monotonic()
        deadline = started + timeout
        result = {"status": "timed_out", "reads": 0, "page": None, "evidence": None,
                  "last_observed_page": None, "additional_verified": None, "read_errors": []}
        phase = "observation"
        try:
            while time.monotonic() < deadline:
                result["reads"] += 1
                try:
                    phase = "observation"
                    page = self.observe(screenshot=False, response_timeout=deadline - time.monotonic())
                    phase = "verification"
                    result.update(status="timed_out", page=page, last_observed_page=page,
                                  evidence=evidence(page, checks), additional_verified=None)
                    phase = "extra_verification"
                    extra = verifier(page) if verifier is not None else True
                    if type(extra) is not bool:
                        raise ValueError("Verifier must return a boolean")
                    result["additional_verified"] = extra if verifier is not None else None
                    if (all(r["state"] == "met" for r in result["evidence"]) and extra
                            and time.monotonic() < deadline):
                        result["status"] = "verified"
                        break
                except Exception as exc:
                    result.update(status="unavailable", page=None, evidence=None, additional_verified=None)
                    result["read_errors"].append({"read": result["reads"], "error": type(exc).__name__, "phase": phase})
                    if not isinstance(exc, (StalePage, TimeoutError)):
                        break
                phase = "settling_wait"
                time.sleep(min(interval, max(0, deadline - time.monotonic())))
        except (KeyboardInterrupt, SystemExit) as exc:
            result.update(status="unavailable", page=None, evidence=None, additional_verified=None)
            result["read_errors"].append({"read": result["reads"], "error": type(exc).__name__, "phase": phase})
            raise
        finally:
            result["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
            if not hasattr(self, "settlements"):
                self.settlements = []
            self.settlements.append(deepcopy(result))
        return deepcopy(result)

    def act(self, action, page, text=None):
        """Compatibility facade; unsafe outcomes must still stop existing agent callers."""
        receipt = self.execute(action, page, text)
        if receipt["status"] == "outcome_unknown":
            raise ExecutionUncertain(receipt)
        if receipt["status"] == "rejected_by_policy":
            error = PolicyRejected("Caller policy rejected observed action before input")
            error.receipt = receipt
            raise error
        if receipt["status"] == "rejected_before_input":
            error = StalePage(f"Action rejected before input during {receipt['phase']} ({receipt.get('error')})")
            error.receipt = receipt
            raise error
        return {"executed": action["id"], **receipt}

    def close(self):
        if self.target:
            self.call("Target.closeTarget", targetId=self.target)
            self.target = None
            self._closed = True


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request, *, transport=None):
    operation = request["operation"]
    session = request["session"]

    def call(method, *, _phase=None, _input=False, **params):
        if transport is not None:
            return transport(method, _phase=_phase, _input=_input, **params)
        return cdp(method, session_id=session, **params)

    def evaluate(expression, *, phase=None):
        kind = request["action"]["kind"] if operation == "act" else None
        mutating = kind in {"select", "scroll_to", "set_range"} and phase != "range_bounds"
        result = call("Runtime.evaluate",
                      _phase=phase or (kind if mutating else "target_resolution" if kind else "snapshot"),
                      _input=mutating, expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            if operation == "act" and request["action"]["kind"] == "scroll_to":
                raise RuntimeError("Scroll execution was interrupted; inspect before retrying.")
            if operation == "act" and request["action"]["kind"] == "set_range" and phase != "range_bounds":
                raise RuntimeError("Range execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", _phase="mouseWheel", _input=True,
                 type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind == "press_key":
            target = evaluate("""(action => {
              const page=""" + READ_STATE + """;
              const e=window.__jevFast?.nodes.get(action.node), focus=document.activeElement;
              const offered=page?.actions.some(a=>a.kind==='press_key' && a.node===action.node &&
                a.key===action.key && a.target_label===action.target_label);
              const focused=action.key==='Enter' ? focus===e && !!e.value?.trim() &&
                e.tagName==='INPUT' && (e.type==='search' || e.getAttribute('role')==='searchbox') &&
                !e.readOnly : action.role==='dialog' ?
                page?.modal_open && e && (focus===e || e.contains(focus)) :
                !page?.modal_open && focus===e && !e.readOnly &&
                (e.tagName==='INPUT' || e.tagName==='TEXTAREA' || e.isContentEditable);
              return {authorized:!!(offered && focused && e?.isConnected &&
                !e.matches(':disabled') && !e.closest('[aria-disabled="true"],[inert]') &&
                e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}))};
            })(""" + json.dumps(action) + ")", phase="key_target")
            if not isinstance(target, dict) or target.get("authorized") is not True:
                raise _TargetRejected("Key target lost focus or is no longer offered")
            code = 13 if action["key"] == "Enter" else 27
            for event in ("keyDown", "keyUp"):
                call("Input.dispatchKeyEvent", _phase=event, _input=True, type=event,
                     key=action["key"], code=action["key"],
                     **({"text": "\r", "unmodifiedText": "\r"}
                        if event == "keyDown" and action["key"] == "Enter" else {}),
                     windowsVirtualKeyCode=code, nativeVirtualKeyCode=code)
        elif kind == "set_range":
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (e?.tagName!=='INPUT' || e.type!=='range' || !e.isConnected || e.readOnly ||
                  e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
                  e.min!==action.min || e.max!==action.max || e.step!==action.step ||
                  e.value!==action.value) return {rejected:true};
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              return x>=0 && y>=0 && x<innerWidth && y<innerHeight &&
                (e===document.elementFromPoint(x,y) || e.contains(document.elementFromPoint(x,y))) ?
                {authorized:true} : {rejected:true};
            })(""" + json.dumps(action) + ")", phase="range_bounds")
            if not isinstance(target, dict) or target.get("authorized") is not True:
                raise _TargetRejected("Range target or bounds changed before input")
            result = evaluate("""(({action,value}) => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (e?.tagName!=='INPUT' || e.type!=='range' || !e.isConnected || e.readOnly ||
                  e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}) ||
                  e.min!==action.min || e.max!==action.max || e.step!==action.step ||
                  e.value!==action.value) return {rejected:true};
              const number=Number(value);
              if (!Number.isFinite(number) || number<Number(e.min) || number>Number(e.max))
                return {rejected:true};
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight ||
                  !(e===document.elementFromPoint(x,y) || e.contains(document.elementFromPoint(x,y))))
                return {rejected:true};
              e.value=value;
              e.dispatchEvent(new Event('input',{bubbles:true}));
              e.dispatchEvent(new Event('change',{bubbles:true}));
              return {set:Number(e.value)===number};
            })(""" + json.dumps({"action": action, "value": request["text"]}) + ")", phase="set_range")
            if isinstance(result, dict) and result.get("rejected") is True:
                raise _TargetRejected("Range changed before mutation")
            if not isinstance(result, dict) or result.get("set") is not True:
                raise RuntimeError("Range mutation was not acknowledged; inspect before retrying")
        elif kind != "wait":
            if type(action["node"]) is not int:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate("""(action => {
              const e=window.__jevFast?.nodes.get(action.node);
              if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
                  !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return {rejected:true};
              if (action.kind==='scroll_to') {
                e.scrollIntoView({behavior:'instant',block:'center',inline:'nearest'});
                return {scrolled:true};
              }
              if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true'))
                return {rejected:true};
              const r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
              if (!r.width || !r.height || x<0 || y<0 || x>=innerWidth || y>=innerHeight) return {rejected:true};
              if (!e.contains(document.elementFromPoint(x,y))) return {rejected:true};
              if (action.kind==='select') {
                if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
                    !o.disabled && !o.closest('optgroup[disabled]'))) return {rejected:true};
                e.value=action.value;
                e.dispatchEvent(new Event('input',{bubbles:true}));
                e.dispatchEvent(new Event('change',{bubbles:true}));
              }
              return {x,y,selected:action.kind==='select'};
            })(""" + json.dumps(action) + ")")
            if isinstance(target, dict) and target.get("rejected") is True:
                raise _TargetRejected("Target changed, is disabled, or is covered. Observe again.")
            if kind == "scroll_to" and (not isinstance(target, dict) or target.get("scrolled") is not True):
                raise RuntimeError("Scroll execution was not confirmed; inspect before retrying.")
            if kind == "select" and (not isinstance(target, dict) or target.get("selected") is not True):
                raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
            if target is None:
                raise StalePage("Target changed or is covered. Observe again.")
            if kind not in {"select", "scroll_to"}:
                x, y = target["x"], target["y"]
                for event in ("mousePressed", "mouseReleased"):
                    call("Input.dispatchMouseEvent", _phase=event, _input=True,
                         type=event, x=x, y=y, button="left", clickCount=1)
                if kind == "fill":
                    def check_focus(phase):
                        bound = evaluate("""(node => {
                          const e=window.__jevFast?.nodes.get(node);
                          const writable=e?.tagName==='INPUT' ?
                            ['text','search','email','url','tel','number'].includes(e.type) :
                            e?.tagName==='TEXTAREA' || (e?.isContentEditable &&
                              !['IFRAME','FRAME','OBJECT','EMBED'].includes(e.tagName));
                          return !!(e?.isConnected && document.activeElement===e && writable &&
                            !e.shadowRoot?.activeElement && !e.readOnly &&
                            e.getAttribute('aria-readonly')!=='true' && !e.matches(':disabled') &&
                            !e.closest('[aria-disabled="true"],[aria-hidden="true"],[inert]') &&
                            e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true}));
                        })(""" + json.dumps(action["node"]) + ")", phase=phase)
                        if bound is not True:
                            raise RuntimeError("Observed field lost focus or writability; text entry stopped")

                    check_focus("focus_after_activation")
                    call(
                        "Input.dispatchKeyEvent", _phase="selectAllDown", _input=True,
                        type="keyDown",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                        commands=["selectAll"],
                    )
                    call(
                        "Input.dispatchKeyEvent", _phase="selectAllUp", _input=True,
                        type="keyUp",
                        key="a",
                        code="KeyA",
                        modifiers=4 if sys.platform == "darwin" else 2,
                    )
                    check_focus("focus_before_insert")
                    call("Input.insertText", _phase="insertText", _input=True, text=request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
