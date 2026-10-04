"""The complete agent loop. Typed choices, observable state, bounded execution."""

import base64
import hashlib
import json
import time
from concurrent.futures import Future
from copy import deepcopy
from pathlib import Path
from threading import BoundedSemaphore, Thread

from .browser import Browser, PolicyRejected, StalePage
from .model import PredictionError, action_space, choose, field_texts, text_context
from .questions import MAX_STEPS


def _decision_evidence(page, context):
    """Only code-owned scalars leave the chooser page; its raw marker never enters a decision."""
    marker = page.get("semantic_marker")
    if marker is None or not isinstance(context, dict) or type(page.get("modal_open")) is not bool:
        return {}
    digest = hashlib.sha256(json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    source = context.get("_source_fingerprint")
    same_observation = (isinstance(source, str) and source == page.get("fingerprint") and
                        context.get("_source_semantic_digest") == digest)
    verification = context.get("verification") if same_observation else None
    checks = context.get("check_evidence_states") if same_observation else None
    if (verification is not None and (not isinstance(verification, list) or
            any(type(v) is not bool for v in verification)) or
            checks is not None and (not isinstance(checks, list) or
            any(v not in {"met", "unmet", "unknown"} for v in checks)) or
            type(context.get("completed_checks_count")) is not int or
            type(context.get("index")) is not int or
            type(context.get("last_mutation_present")) is not bool):
        return {}
    return {"state_digest": digest, "state_context": {
        "modal_open": page["modal_open"], "verification": list(verification) if verification is not None else None,
        "check_evidence_states": list(checks) if checks is not None else None,
        "completed_checks_count": context["completed_checks_count"], "index": context["index"],
        "last_mutation_present": context["last_mutation_present"],
    }}


class Agent:
    def __init__(self, url, goals, *, record_dir=None, screenshots=False, speculative_text=False, text_provider=None):
        task = goals.strip() if isinstance(goals, str) else "\n".join(goals).strip()
        if not task:
            raise ValueError("Supply a task")
        plan = [task]
        self.pending_text = None
        self.speculative_text = speculative_text
        self.text_provider = text_provider
        self.execution_context = None
        self.decision_context = None  # Controller-owned telemetry, never passed into the model request.
        self.binding_provider = None
        self.binding_choices = {}
        self.selected_binding_choice = None
        self._text_slots = BoundedSemaphore(2)  # Cap uncancellable in-flight HTTP calls.
        self.browser = Browser(url)
        self.record_dir = Path(record_dir) if record_dir else None
        self.screenshots = screenshots or bool(record_dir)
        try:
            page = self.browser.observe(screenshot=self.screenshots)
        except Exception:
            self.browser.close()
            raise
        self.state = dict(
            browser=self.browser,
            goal="\n".join(plan),
            page=page,
            decision=None,
            history=[],
            attempts=[],
            status="ready",
            stop_reason=None,
            stale_retries=0,
            plan=plan,
            plan_index=0,
            decisions=[],
            prediction_calls=[],
            text_calls=[],
            elapsed_ms=0,
            started_at=None,
            record=bool(self.record_dir),
        )
        if self.record_dir:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            (self.record_dir / "000000.jpg").write_bytes(base64.b64decode(page["screenshot"]))

    def snapshot(self):
        return {
            **{k: v for k, v in self.state.items() if k != "browser"},
            "elements": action_space(self.state["page"]["actions"])[0],
            "prediction_calls": deepcopy(self.state["prediction_calls"]),
            "browser_calls": getattr(self.browser, "cdp_calls", [])
                             if isinstance(getattr(self.browser, "cdp_calls", None), list) else [],
        }

    def discard_text(self):
        if self.pending_text:
            _, future, record = self.pending_text
            if future and future.cancel():
                record["cancelled"] = True
            self.pending_text = None

    def prepare_text(self, context):
        if self.text_provider is not None:
            self.discard_text()
            return
        if self.pending_text and self.pending_text[0] == context:
            return
        self.discard_text()
        if not context["fields"]:
            return
        if not self.speculative_text:
            self.pending_text = (context, None, None)
            return
        if not self._text_slots.acquire(blocking=False):
            # Saturation cannot spawn more speculative requests; TYPE_TEXT can still prepare synchronously.
            self.pending_text = (context, None, None)
            return
        future = Future()
        record = {"fields": len(context["fields"]), "used": False, "started": False}
        self.state["text_calls"].append(record)
        self.pending_text = (context, future, record)

        def generate():
            try:
                if future.set_running_or_notify_cancel():
                    record["started"] = True
                    try:
                        future.set_result(field_texts(context))
                    except Exception as error:
                        record["error"] = type(error).__name__
                        future.set_exception(error)
            finally:
                self._text_slots.release()

        Thread(target=generate, daemon=True).start()

    def command(self, name, body=None):
        body = body or {}
        state = self.state
        if name == "tick":
            try:
                self.command("predict", {})
                return self.command("act", {"fingerprint": state["page"]["fingerprint"]})
            except StalePage:
                state["stale_retries"] = state.get("stale_retries", 0) + 1
                state["decision"] = None
                state["status"] = "ready"
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                return self.snapshot()
        elif name == "predict":
            if not state["browser"]:
                raise ValueError("Start a demo first")
            if state["status"] in {"done", "blocked", "needs_attention"}:
                raise ValueError("This run has stopped. Start a fresh demo.")
            if state["started_at"] is None:
                state["started_at"] = time.perf_counter()
            if not state["browser"].fresh(state["page"]):
                state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["decision"] = None
            if len(state["decisions"]) >= MAX_STEPS * 2:
                state.update(status="blocked", stop_reason="model_budget")
                self.discard_text()
                raise ValueError("Reached the demo's model-call budget")
            self.prepare_text(text_context(state["goal"], state["page"], state["history"]))
            call = {"status": "started"}
            state["prediction_calls"].append(call)
            started = time.perf_counter()
            try:
                decision_evidence = _decision_evidence(state["page"], getattr(self, "decision_context", None))
                provider = getattr(self, "binding_provider", None)
                self.binding_choices = provider(state["page"]) if provider else {}
                if self.execution_context is None:
                    state["decision"] = choose(state["page"], state["goal"], state["history"])
                else:
                    execution = self.execution_context
                    if self.binding_choices:
                        execution = {**execution, "_binding_choices": self.binding_choices}
                    state["decision"] = choose(state["page"], state["goal"], state["history"],
                                               execution=execution)
                call["status"] = "returned"
            except (Exception, KeyboardInterrupt, SystemExit) as exc:
                call.update(status="error", error=type(exc).__name__)
                if isinstance(exc, PredictionError):
                    call["diagnostic"] = deepcopy(exc.diagnostic)
                raise
            finally:
                self.decision_context = None
                call["latency_ms"] = round((time.perf_counter() - started) * 1000)
            state["decisions"].append(
                {
                    **state["decision"], **decision_evidence,
                    "fingerprint": state["page"]["fingerprint"],
                    "elapsed_ms": round((time.perf_counter() - state["started_at"]) * 1000),
                }
            )
            state["status"] = "predicted"
        elif name == "act":
            decision, page = state["decision"], state["page"]
            if not decision or body.get("fingerprint") != page["fingerprint"]:
                raise ValueError("Observe and choose before acting")
            # Consume once, before any mutation or model call. A retry cannot double-click.
            self.selected_binding_choice = decision.get("binding_choice")
            state["decision"] = None
            selected = decision["choice"]
            if selected in {"DONE", "BLOCKED"}:
                if not state["browser"].fresh(page, terminal=True):
                    state["status"] = "ready"
                    raise StalePage("Page changed since the decision. Choose again.")
                state["status"] = "done" if selected == "DONE" else "blocked"
                state["stop_reason"] = "model_done" if selected == "DONE" else "model_blocked"
                state["plan_index"] = int(selected == "DONE")
                state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
                self.discard_text()
                return self.snapshot()
            action = next(a for a in page["actions"] if a["id"] == selected)
            if action["kind"] == "scroll_to" and any(
                h.get("kind") == "scroll_to" and h.get("node") == action["node"] and h.get("url") == page["url"]
                for h in state["history"]
            ):
                raise ValueError("Already attempted to reveal this element; scroll mutation not retried.")
            if len(state["history"]) >= MAX_STEPS:
                state.update(status="blocked", stop_reason="action_budget")
                self.discard_text()
                raise ValueError(f"Stopped at the {MAX_STEPS}-action demo budget")
            text, helper = None, None
            if action["kind"] in {"fill", "set_range"}:
                if not state["browser"].fresh(page):
                    raise StalePage("Page changed before prepared input. Choose again.")
                if self.text_provider is not None:
                    text = self.text_provider(action, page)
                    if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                        raise ValueError("Prepared text is invalid; nothing typed.")
                else:
                    if action["kind"] == "set_range":
                        raise ValueError("SET_RANGE requires a caller-prepared value; no helper fallback.")
                    context = text_context(state["goal"], page, state["history"])
                    if not self.pending_text or self.pending_text[0] != context:
                        raise ValueError("Text was not prepared for this page; nothing typed.")
                    _, future, record = self.pending_text
                    if future is None:
                        selected_context = {**context, "fields": {action["id"]: context["fields"][action["id"]]}}
                        record = {"fields": 1, "used": False, "started": True}
                        state["text_calls"].append(record)
                        if not self._text_slots.acquire(timeout=30):
                            raise RuntimeError("Text preparation is busy; nothing typed.")
                        try:
                            values, helper = field_texts(selected_context)
                        except Exception as error:
                            record["error"] = type(error).__name__
                            raise
                        finally:
                            self._text_slots.release()
                        cached = Future()
                        cached.set_result((values, helper))
                        self.pending_text = (context, cached, record)
                    else:
                        values, helper = future.result()
                    text = values.get(action["id"])
                    if not text:
                        raise ValueError("Text helper returned no value for the selected field; nothing typed.")
                    record.update(**helper, field=action["label"], value=text, used=True)
            event = {
                "step": len(state["history"]) + 1,
                "action": action["label"],
                "kind": action["kind"],
                "choice": selected,
                "probability": decision["probabilities"][selected],
                "confidence": decision["confidence"],
                "latency_ms": decision["latency_ms"],
                "text": text,
                "text_source": "prepared" if text is not None and self.text_provider is not None else
                               "helper" if helper else None,
                "text_helper": helper["model"] if helper else None,
                "text_latency_ms": helper["latency_ms"] if helper else 0,
                "operation": decision["operation"],
                "target": decision["target"],
                "page_changed": None,
                "from_url": page["url"],
                "url": page["url"],
                "usage": decision["usage"],
            }
            # A scroll can take effect even if its CDP reply is interrupted. Record the attempt first.
            if action["kind"] == "scroll_to":
                event.update(attempted=True, node=action["node"])
                state["history"].append(event)
            attempt = {"choice": selected, "kind": action["kind"], "action": action["label"], "status": "issued"}
            state.setdefault("attempts", []).append(attempt)
            try:
                receipt = state["browser"].act(action, page, text=text)
            except PolicyRejected as exc:
                attempt.update(status="rejected_by_policy", error=type(exc).__name__)
                if getattr(exc, "receipt", None):
                    attempt["receipt"] = exc.receipt
                if action["kind"] == "scroll_to":
                    state["history"].pop()
                state["status"] = "ready"
                raise
            except StalePage as exc:
                attempt.update(status="rejected_before_input", error=type(exc).__name__)
                if getattr(exc, "receipt", None):
                    attempt["receipt"] = exc.receipt
                if action["kind"] == "scroll_to":
                    state["history"].pop()  # The guard rejected it before any scroll.
                raise
            except (Exception, KeyboardInterrupt, SystemExit) as exc:
                attempt.update(status="outcome_unknown", error=type(exc).__name__)
                receipt = getattr(exc, "receipt", None)
                receipts = getattr(state["browser"], "receipts", [])
                if receipt is None and isinstance(receipts, list) and receipts:
                    receipt = receipts[-1]
                if receipt is not None:
                    attempt["receipt"] = receipt
                state.update(status="needs_attention", stop_reason="execution_error")
                raise
            attempt["status"] = "executed"
            if isinstance(receipt, dict) and "status" in receipt:
                attempt["receipt"] = receipt
                event["receipt"] = receipt
            self.discard_text()
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            event.update(executed_ms=state["elapsed_ms"], elapsed_ms=state["elapsed_ms"])
            event.pop("attempted", None)
            # Record successful execution before observing. A stale post-action read cannot erase it.
            if action["kind"] != "scroll_to":
                state["history"].append(event)
            state["page"] = state["browser"].observe(screenshot=self.screenshots)
            state["elapsed_ms"] = round((time.perf_counter() - state["started_at"]) * 1000)
            state["history"][-1].update(
                page_changed=(state["page"].get("semantic_marker", state["page"]["fingerprint"]) !=
                              page.get("semantic_marker", page["fingerprint"])),
                url=state["page"]["url"],
                elapsed_ms=state["elapsed_ms"],
            )
            if state["record"]:
                (self.record_dir / f"{state['elapsed_ms']:06d}.jpg").write_bytes(
                    base64.b64decode(state["page"]["screenshot"])
                )
            repeated = state["history"][-3:]
            no_progress = (
                len(repeated) == 3
                and all(h["page_changed"] is False and h["kind"] != "wait" for h in repeated)
                and len({(h["kind"], h["choice"]) for h in repeated}) == 1
            )
            state["status"] = "blocked" if no_progress else "ready"
            state["stop_reason"] = "no_progress" if no_progress else None
        else:
            raise ValueError("Unknown command")
        return self.snapshot()

    def run(self):
        while self.state["status"] not in {"done", "blocked", "needs_attention"}:
            yield self.command("tick")

    def close(self):
        self.discard_text()
        self.browser.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
