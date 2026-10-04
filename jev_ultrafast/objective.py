"""A prepared-plan loop: Jev acts, code verifies; runtime replanning is opt-in."""

import hashlib
import json
import math
import time
import unicodedata
from copy import deepcopy
from dataclasses import asdict, replace

from .agent import Agent
from .browser import PolicyRejected, StalePage
from .model import PredictionError, _mutation_key, action_space
from .planning import Check, PlanningError, Text, controls, evidence, parse_plan, plan_objective, resolve_controls


class PreparedTextUnavailable(ValueError):
    """Raised before mutation when a prepared value cannot bind uniquely to an observed field."""


class RepeatedMutation(ValueError):
    """Raised before input when the same mutation would be repeated without new observed evidence."""


class ObjectiveAgent:
    def __init__(self, url, goal, *, checks, prepared_plan=None, planner=None,
                 max_replans=0, local_attempts=2, max_decisions=24, max_actions=24, max_seconds=90,
                 settle_timeout=5, verifier=None, recover_bindings=False, late_bindings=False):
        self.checks = tuple(checks)
        if not self.checks or any(not isinstance(c, Check) for c in self.checks):
            raise ValueError("Supply nonempty caller-owned final checks")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 2000:
            raise ValueError("Supply one bounded original objective")
        if any(type(v) is not int or v < 0 for v in (max_replans, local_attempts)):
            raise ValueError("Invalid recovery limits")
        if any(type(v) is not int or v < 1 for v in (max_decisions, max_actions, max_seconds)):
            raise ValueError("Invalid execution limits")
        if (type(settle_timeout) not in {int, float} or not math.isfinite(settle_timeout)
                or settle_timeout < 0):
            raise ValueError("Invalid outcome settling limit")
        if settle_timeout and type(self).observe is not ObjectiveAgent.observe:
            raise ValueError("Override verify_observation for full verification, not observe; "
                             "or disable automatic settling with settle_timeout=0")
        if verifier is not None and not callable(verifier):
            raise ValueError("Invalid outcome verifier")
        if type(recover_bindings) is not bool:
            raise ValueError("Invalid binding recovery policy")
        if type(late_bindings) is not bool:
            raise ValueError("Invalid late binding policy")
        self.recover_bindings = recover_bindings
        self.late_bindings = late_bindings
        self.value_owners = {}  # (source step, text index) -> acknowledged, freshly proven node.
        self.pending_binding = None
        self.verifier = verifier
        self.additional_verified = None
        self.settle_timeout = settle_timeout
        self.settled_generation = None
        self.goal = goal.strip()
        self.plan = parse_plan(prepared_plan, objective=self.goal) if prepared_plan is not None else None
        self.planner = planner or plan_objective
        self.max_replans, self.local_attempts = max_replans, local_attempts
        self.max_decisions, self.max_actions, self.max_seconds = max_decisions, max_actions, max_seconds
        self.replans = 0
        self.local_used = 0
        self.stale_failures = 0
        self.blocker_key = None
        self.blocker_attempts = 0
        self.index = 0
        self.status, self.stop_reason = "ready", None
        self.verification = None
        self.check_evidence = None
        self.verified_steps = set()
        self.deferred_steps = set()
        self.planner_calls = []
        self.events = []
        self.feedback = None
        self._phase = "initialization"
        self.last_mutation = None
        self.forbidden_targets = set()
        self.progress_document = None
        self.completed_checks = set()
        self.agent = Agent(url, self.goal, text_provider=self.prepared_value)
        self.agent.binding_provider = self.binding_candidates if late_bindings else None
        self.browser = self.agent.browser
        self.started = time.perf_counter()

    def snapshot(self):
        return {**self.agent.snapshot(), "goal": self.goal, "status": self.status, "stop_reason": self.stop_reason,
                "model_status": self.agent.state["status"],
                "verification": list(self.verification) if self.verification is not None else None,
                "check_evidence": deepcopy(self.check_evidence) if self.verification is not None else None,
                "additional_verified": self.additional_verified if self.verification is not None else None,
                "verified_steps": sorted(self.verified_steps), "deferred_steps": sorted(self.deferred_steps),
                "corrections_used": self.local_used, "replans_used": self.replans,
                "planner_calls": list(self.planner_calls), "events": deepcopy(self.events),
                "plan": [s.goal for s in self.plan.steps] if self.plan else [], "plan_index": self.index,
                "elapsed_ms": round((time.perf_counter() - self.started) * 1000)}

    def observe(self):
        self.verification = self.additional_verified = None
        if self.status == "done":
            self.status, self.stop_reason = "ready", None
        self._phase = "observation"
        page = self.browser.observe(screenshot=False)
        self._phase = "verification"
        self.verify_observation(page)
        return page

    def verify_observation(self, page):
        """Trusted caller verification hook, also used on each settling snapshot without a second read."""
        if self.status == "done":
            self.status, self.stop_reason = "ready", None
        self._phase = "verification"
        self.agent.state["page"] = page
        self.check_evidence = evidence(page, self.checks)
        self.verification = [r["state"] == "met" for r in self.check_evidence]
        self.additional_verified = None
        if self.verifier is not None:
            self._phase = "extra_verification"
            extra = self.verifier(deepcopy(page))
            if type(extra) is not bool:
                raise ValueError("Outcome verifier must return a boolean")
            self.additional_verified = extra
        self._phase = "milestone_verification"
        self.release_owners(page)
        document = page.get("document_id", page.get("page_key", [None])[0])
        if document != self.progress_document:
            self.completed_checks.clear()
            self.progress_document = document
        for check in tuple(self.completed_checks):
            result = evidence(page, (check,))[0]
            if result["state"] == "unmet" or (result["state"] == "unknown" and
                    (page.get("modal_open") is not True or result["reason"] not in
                     {"missing_control", "obscured_control"})):
                self.completed_checks.discard(check)
        if self.plan:
            while self.index < len(self.plan.steps):
                self.ground_milestone(page)
                checks = self.plan.steps[self.index].checks
                proof = evidence(page, checks)
                grounded = {}
                for i, check in enumerate(checks):
                    bound = self.bound_milestone(check, page)
                    if bound is not None and proof[i]["state"] == "unknown" and proof[i]["reason"] in {
                            "missing_control", "obscured_control"}:
                        proof[i] = evidence(page, (bound,))[0]
                        grounded[i] = bound
                results = [r["state"] == "met" for r in proof]
                preserved = [r["state"] == "unknown" and r["reason"] in {"missing_control", "obscured_control"}
                             and document is not None and page.get("modal_open") is True
                             and check in self.completed_checks for check, r in zip(checks, proof, strict=True)]
                if not all(met or old for met, old in zip(results, preserved, strict=True)):
                    break
                self.completed_checks.update(grounded.get(i, c)
                                             for i, (c, met) in enumerate(zip(checks, results, strict=True))
                                             if met and c.kind in {"value", "checked"})
                self.events.append({"kind": "milestone_preserved" if any(preserved) else "milestone_verified",
                                    "index": self.index})
                self.verified_steps.add(self.index)
                self.index += 1
                self.feedback = None
        if all(self.verification) and (self.verifier is None or self.additional_verified is True):
            self.status, self.stop_reason = "done", "verified"
            self.agent.discard_text()
        elif self.status == "done":
            self.status, self.stop_reason = "ready", None
        return self.status == "done"

    def ground_milestone(self, page):
        """A query is not a selected value. Prefer explicit caller canonical spelling, never fuzzy success."""
        step = self.plan.steps[self.index]
        checks, changed = list(step.checks), []
        for index, check in enumerate(checks):
            if check.kind != "value":
                continue
            nodes = controls(page, check)
            if len(nodes) != 1:
                continue
            node = next(iter(nodes))
            if not any(a.get("node") == node and a["kind"] == "fill" and a.get("role") == "combobox"
                       for a in page["actions"]):
                continue
            texts = [t for t in step.texts if set(controls(page, t)) == {node}]
            finals = [c for c in self.checks if c.kind == "value" and set(controls(page, c)) == {node}]
            if len(texts) != 1 or texts[0].value != check.value or len(finals) != 1:
                continue
            canonical = finals[0].value
            query_key = "".join(c for c in unicodedata.normalize("NFD", check.value) if not unicodedata.combining(c))
            canonical_key = "".join(c for c in unicodedata.normalize("NFD", canonical) if not unicodedata.combining(c))
            if canonical == check.value or query_key != canonical_key:
                continue
            checks[index] = replace(check, value=canonical)
            changed.append(index)
        if changed:
            steps = list(self.plan.steps)
            steps[self.index] = replace(step, checks=tuple(checks))
            self.plan = replace(self.plan, steps=tuple(steps))
            self.events.append({"kind": "milestone_grounded", "index": self.index, "check_indices": changed,
                                "source": "caller_final_check"})

    def planning_context(self, reason):
        page = self.agent.state["page"]
        return {"objective": self.goal, "final_checks": [asdict(c) for c in self.checks],
                "verification": list(self.verification), "check_evidence": self.check_evidence, "reason": reason,
                "additional_verified": self.additional_verified,
                "verified_control_progress": [asdict(c) for c in sorted(self.completed_checks, key=repr)],
                "page": {"url": page["url"], "title": page["title"], "text": page["text"][:3000],
                         "modal_open": bool(page.get("modal_open"))},
                "elements": action_space(page["actions"])[0],
                "prior_plan": {"objective": self.plan.objective, "plan": [asdict(s) for s in self.plan.steps]}
                              if self.plan else None, "completed_steps": len(self.verified_steps),
                "preferred_step_index": self.index, "deferred_steps": sorted(self.deferred_steps),
                "history": [{k: h.get(k) for k in ("action", "kind", "text", "from_url", "url",
                                                  "page_changed", "attempted")}
                            for h in self.agent.state["history"][-5:]],
                "recent_decisions": [{k: d.get(k) for k in ("operation", "choice", "confidence")}
                                     for d in self.agent.state["decisions"][-3:]]}

    def request_plan(self, reason):
        event = {"phase": "initial" if self.plan is None else "replan", "reason": reason}
        self.planner_calls.append(event)
        started = time.perf_counter()
        try:
            context = self.planning_context(reason)
            event["context_bytes"] = len(json.dumps(context, ensure_ascii=False).encode())
            data, meta = self.planner(context)
            plan = parse_plan(data, objective=self.goal)
            if not isinstance(meta, dict):
                raise ValueError("Invalid planning metadata")
        except Exception as exc:
            event.update(error=type(exc).__name__, latency_ms=round((time.perf_counter() - started) * 1000))
            if isinstance(exc, PlanningError):
                event["diagnostic"] = dict(exc.diagnostic)
            self.status, self.stop_reason = "needs_attention", "planning_failed"
            return
        event.update({k: meta[k] for k in ("model", "latency_ms", "usage", "transport") if k in meta})
        event["plan"] = deepcopy(data)  # Preserve each validated source contract before runtime grounding.
        self.plan, self.index = plan, 0
        self.value_owners.clear()
        self.pending_binding = None
        self.verified_steps.clear()
        self.deferred_steps.clear()
        self.feedback = None

    def defer_milestone(self, reason):
        """Move the preferred-goal cursor, not the completion evidence."""
        if not self.plan or self.index >= len(self.plan.steps):
            return False
        self.events.append({"kind": "milestone_deferred", "index": self.index, "reason": reason,
                            "evidence": evidence(self.agent.state["page"], self.plan.steps[self.index].checks)})
        self.deferred_steps.add(self.index)
        self.index += 1
        self.feedback = {"reason": "advisory_milestone", "instruction":
                         "Continue toward the original objective; deferred checks are NOT verified."}
        self.agent.state.update(status="ready", stop_reason=None, decision=None)
        return True

    @staticmethod
    def known_document(document):
        return (isinstance(document, str) and bool(document.strip()) or
                type(document) in {int, float} and math.isfinite(document))

    def release_owners(self, page):
        """Unknown identity is not proof of replacement; keep ownership until a known contradiction."""
        document = page.get("document_id")
        if not self.known_document(document):
            return
        for slot, owner in tuple(self.value_owners.items()):
            if owner["document"] != document:
                del self.value_owners[slot]
                continue
            facts = [c for c in page.get("controls", page["actions"])
                     if c.get("node") == owner["node"] and c.get("observable") is True]
            if (len(facts) == 1 and isinstance(facts[0].get("value"), str) and
                    facts[0]["value"] != owner["source"].value):
                del self.value_owners[slot]

    def bound_milestone(self, check, page, *, step_index=None):
        """Only the acknowledged source slot can ground its own current-step value predicate."""
        index = self.index if step_index is None else step_index
        for (source_step, _), owner in self.value_owners.items():
            text = owner["source"]
            if (source_step != index or page.get("document_id") != owner["document"] or
                    check != Check("value", text.value, text.label, text.role, text.group)):
                continue
            target = owner["target"]
            bound = Check("value", text.value, target.label, target.role, target.group)
            nodes = controls(page, bound, facts=True)
            if len(nodes) == 1 and owner["node"] in nodes and nodes[owner["node"]].get("observable") is True:
                return bound
        return None

    def binding_candidates(self, page):
        """One finite source-slot set per freshly offered unbound fill; no executable selectors."""
        document = page.get("document_id")
        if (not self.late_bindings or not self.plan or self.index >= len(self.plan.steps) or
                not self.known_document(document) or
                not isinstance(page.get("controls"), list) or page.get("modal_open") is not False or
                any(type(page.get(k)) is not int or page[k] != 0
                    for k in ("omitted_actions", "omitted_controls"))):
            return {}
        step = self.plan.steps[self.index]
        if sum(a.get("kind") == "fill" for a in page["actions"]) > 8:
            return {}  # Fail closed rather than silently omit speculative heads for some targets.
        candidates = {}
        for action in page["actions"]:
            if (action.get("kind") != "fill" or action.get("role") not in {"textbox", "combobox", "searchbox"} or
                    type(action.get("node")) is not int or not isinstance(action.get("value"), str) or
                    not isinstance(action.get("label"), str) or not action["label"].strip() or
                    len(action["label"]) > 500 or sum(a == action for a in page["actions"]) != 1 or
                    sum(a.get("node") == action["node"] and a.get("kind") == "fill"
                        for a in page["actions"]) != 1 or
                    any(o["node"] == action["node"] for o in self.value_owners.values())):
                continue
            options = {}
            for text_index, text in enumerate(step.texts):
                slot = (self.index, text_index)
                if slot in self.value_owners or (text.role and text.role != action["role"]) or (
                        text.group and " ".join(text.group.split()) != " ".join(action.get("group", "").split())):
                    continue
                predicate = Check("value", text.value, text.label, text.role, text.group)
                matches = sum(c == predicate for c in step.checks)
                if matches > 1 or (matches == 0 and any(c.kind in {"value", "checked"} for c in step.checks)):
                    continue
                offered, audit = resolve_controls(page, text, recover=self.recover_bindings)
                original, _ = resolve_controls(page, text, facts=True)
                if (offered or audit["method"] == "ambiguous" or len(original) > 1 or
                        any(c.get("observable") is not False for c in original.values())):
                    continue
                target = Text(action["label"], text.value, role=text.role or action["role"], group=text.group)
                facts, _ = resolve_controls(page, target, facts=True)
                if len(facts) != 1 or action["node"] not in facts:
                    continue
                fact = facts[action["node"]]
                if (fact.get("observable") is not True or fact.get("disabled") is not False or
                        fact.get("readonly") is not False or fact.get("role") != action["role"] or
                        " ".join(fact.get("group", "").split()) != " ".join(action.get("group", "").split()) or
                        fact.get("value") != action["value"] or action["value"] == text.value):
                    continue
                options[f"s{self.index}t{text_index}"] = {"value": text.value, "label": text.label}
            if options:
                candidates[action["id"]] = options
        return candidates

    def prepared_range_value(self, action, page):
        """A range uses one exact current-step prepared source; no generated or fuzzy value."""
        if (action.get("kind") != "set_range" or action.get("role") != "slider" or
                not self.plan or self.index >= len(self.plan.steps)):
            raise PreparedTextUnavailable("Range has no current prepared value.")
        matches = []
        for text in self.plan.steps[self.index].texts:
            observed, _ = resolve_controls(page, text)
            if len(observed) == 1 and action["node"] in observed:
                matches.append(text.value)
        if len(matches) != 1:
            raise PreparedTextUnavailable("Range needs one uniquely bound current-step prepared value.")
        if action.get("value") == matches[0]:
            raise RepeatedMutation("Range already contains the prepared value.")
        return matches[0]

    def prepared_value(self, action, page):
        if (getattr(self, "late_bindings", False) and getattr(self, "value_owners", {}) and
                not self.known_document(page.get("document_id"))):
            raise PreparedTextUnavailable("Document identity is unknown; owned values remain reserved.")
        if action.get("kind") == "set_range":
            return self.prepared_range_value(action, page)
        bindings = []
        if self.plan:
            order = ([self.index] if self.index < len(self.plan.steps) else []) + [
                i for i in range(len(self.plan.steps)) if i != self.index]
            for step_index in order:
                for text_index, text in enumerate(self.plan.steps[step_index].texts):
                    observed, audit = resolve_controls(page, text, recover=self.recover_bindings)
                    if len(observed) == 1 and action["node"] in observed:
                        bindings.append(((step_index, text_index), text, audit))
                if bindings:
                    break  # Current-step exact/normalized binding always takes precedence.
        if len(bindings) > 1:
            raise PreparedTextUnavailable("Ambiguous exact prepared bindings; nothing typed.")
        if len(bindings) == 1:
            slot, text, audit = bindings[0]
            owner = self.value_owners.get(slot)
            if owner and (owner["node"] != action["node"] or owner["document"] != page.get("document_id")):
                raise PreparedTextUnavailable("Owned source slot cannot move to another field.")
        else:
            token = self.agent.selected_binding_choice
            options = self.agent.binding_choices.get(action["id"], {})
            fresh = self.binding_candidates(page).get(action["id"], {})
            if (type(token) is not str or token == "NONE" or token not in options or
                    options[token] != fresh.get(token)):
                raise PreparedTextUnavailable("No validated finite prepared value belongs to this field.")
            prefix, separator, suffix = token.partition("t")
            if not separator or not prefix.startswith("s") or not prefix[1:].isdigit() or not suffix.isdigit():
                raise PreparedTextUnavailable("Invalid source slot.")
            slot = (int(prefix[1:]), int(suffix))
            if slot[0] != self.index or slot[1] >= len(self.plan.steps[self.index].texts):
                raise PreparedTextUnavailable("Source slot no longer belongs to the current step.")
            text = self.plan.steps[slot[0]].texts[slot[1]]
            audit = {"method": "finite_choice", "node": action["node"],
                     "binding_step": slot[0], "binding_text": slot[1]}
            self.pending_binding = {"slot": slot, "source": text,
                                    "target": Text(action["label"], text.value, action["role"], text.group),
                                    "node": action["node"], "document": page["document_id"]}
            self.events.append({"kind": "binding_late_bound", **audit})
        if action.get("value") == text.value:
            raise RepeatedMutation("Field already contains the prepared value; choose another action.")
        if audit["method"] == "edit_distance":
            self.events.append({"kind": "binding_recovered", **audit, "binding_step": slot[0],
                                "binding_text": slot[1], "plan_request": len(self.planner_calls) - 1
                                if self.planner_calls else None})
        return text.value

    def repeated_mutation(self):
        state = self.agent.state
        if self.last_mutation is None or not state["decision"]:
            return False
        action = next((a for a in state["page"]["actions"] if a["id"] == state["decision"]["choice"]), {})
        if action.get("kind") not in {"click", "select", "press_key", "set_range"}:
            return False
        current = state["page"].get("semantic_marker", state["page"].get("marker", state["page"].get("fingerprint")))
        key = _mutation_key(action)
        if action["kind"] == "set_range":
            try:
                key = ("set_range", action["node"], self.prepared_range_value(action, state["page"]))
            except (PreparedTextUnavailable, RepeatedMutation):
                return False  # Prepared-value validation itself stops input on the act path.
        return self.last_mutation == (key, current, current)

    def limit_stop(self):
        state = self.agent.state
        reason = None
        if time.perf_counter() - self.started >= self.max_seconds:
            reason = "time_budget"
        elif len(state["decisions"]) >= self.max_decisions:
            reason = "decision_budget"
        elif len(state["history"]) >= self.max_actions:
            reason = "action_budget"
        if reason:
            self.status, self.stop_reason = "abandoned", reason
            self.agent.discard_text()
            self.events.append({"kind": "abandoned", "reason": reason,
                                "verification": list(self.verification),
                                "scope": "Configured execution limit; not proof of impossibility"})
        return reason is not None

    def awaiting_load(self):
        if self.agent.state["page"].get("ready_state", "complete") not in {"loading", "interactive"}:
            return False
        self.feedback = {"reason": "page_loading", "instruction": "Wait for document readiness before correction."}
        self.events.append({"kind": "awaiting_load"})
        time.sleep(.2)
        return True

    def recover(self, reason, *, target=None):
        if self.limit_stop():
            return
        self.agent.discard_text()
        self.agent.state.update(status="ready", stop_reason=None, decision=None)
        if self.awaiting_load():
            return
        self.feedback = {"reason": reason, "verification": list(self.verification),
                         "instruction": "Objective is unverified; choose a supported corrective action, not a repeat."}
        if target is not None:
            self.feedback["target"] = target
        page = self.agent.state["page"]
        key = (reason if reason in {"missing_prepared_text", "policy_rejected"} else "no_progress",
               repr(page.get("semantic_marker", page.get("marker", page.get("fingerprint")))),
               tuple(self.verification), self.additional_verified,
               tuple((r["state"], r["reason"]) for r in self.check_evidence))
        if key != self.blocker_key:
            self.blocker_key, self.blocker_attempts = key, 0
        self.blocker_attempts += 1
        if self.blocker_attempts <= self.local_attempts:
            self.local_used += 1
            self.events.append({"kind": "local_correction", "reason": reason,
                                "stable_observations": self.blocker_attempts,
                                "verification": list(self.verification)})
            return
        if reason == "policy_rejected" or self.replans >= self.max_replans:
            self.status, self.stop_reason = "abandoned", (
                "policy_exhausted" if reason == "policy_rejected" else "replanning_exhausted")
            self.events.append({"kind": "abandoned", "reason": reason,
                                "verification": list(self.verification),
                                "scope": "Unresolved within supported actions and configured recovery limits"})
            return
        self.replans += 1
        self.request_plan(reason)

    def settle_after_stale(self, rejected_page):
        """At most three extra reads. Stable changed context permits a new decision, never replay."""
        rejected_marker = rejected_page.get(
            "semantic_marker", rejected_page.get("marker", rejected_page.get("fingerprint")))
        page = self.observe()
        previous = page.get("semantic_marker", page.get("marker", page.get("fingerprint")))
        for _ in range(3):
            if self.status != "ready" or self.limit_stop():
                return True
            time.sleep(.05)
            page = self.observe()
            current = page.get("semantic_marker", page.get("marker", page.get("fingerprint")))
            if current == previous and page.get("ready_state", "complete") == "complete":
                if current != rejected_marker:
                    self.feedback = {"reason": "page_settled", "instruction": "Choose from the fresh observation."}
                    self.events.append({"kind": "pre_input_settled"})
                    return True
                return False
            previous = current
        return False

    def settle_objective(self):
        """One read-only window per confirmed mutation generation, before objective-level DONE recovery."""
        generation = sum(h.get("kind") != "wait" for h in self.agent.state["history"])
        if self.settle_timeout == 0 or self.settled_generation == generation:
            return False
        remaining = self.max_seconds - (time.perf_counter() - self.started)
        if remaining <= 0:
            self.limit_stop()
            return True
        timeout = min(self.settle_timeout, remaining)
        self.settled_generation = generation
        self.events.append({"kind": "outcome_settling", "generation": generation, "timeout": timeout})
        self._phase = "outcome_settling"
        result = self.browser.settle(self.checks, timeout=timeout, verifier=self.verify_observation)
        event = {"kind": "outcome_settled", "generation": generation,
                 **{k: result[k] for k in ("status", "reads", "elapsed_ms", "read_errors")}}
        if result["status"] == "unavailable" and result["read_errors"]:
            phase = result["read_errors"][-1].get("phase", "outcome_settling")
            event["failure_phase"] = self._phase if phase == "extra_verification" else phase
        self.events.append(event)
        if result["status"] == "verified":
            self.status, self.stop_reason = "done", "verified"
            return True
        if result["status"] == "unavailable":
            self.verification = self.check_evidence = self.additional_verified = None
            self.status, self.stop_reason = "needs_attention", "observation_unavailable"
            self.agent.state.update(status="needs_attention", decision=None)
            return True
        if self.status == "done":  # A late predicate cannot bypass the settling/time budget.
            self.status, self.stop_reason = "ready", None
        return self.limit_stop()

    def _tick(self):
        self.observe()
        self._phase = "controller_policy"
        if self.status == "done" or self.limit_stop() or self.awaiting_load():
            return
        if self.plan is None:
            self.request_plan("initial")
            if self.status != "ready":
                return
            self.observe()
            if self.status == "done" or self.limit_stop() or self.awaiting_load():
                return
        self._phase = "decision_context"
        step_index = self.index
        step = self.plan.steps[self.index] if self.index < len(self.plan.steps) else None
        self.agent.state["goal"] = step.goal if step else self.goal
        page = self.agent.state["page"]
        offered_nodes = {a.get("node") for a in page["actions"]}
        unsupported_controls = [
            {"role": c["role"][:40], "label": c["label"][:120],
             **({"group": c["group"][:80]} if isinstance(c.get("group"), str) else {}),
             "disabled": c.get("disabled") is True, "readonly": c.get("readonly") is True}
            for c in page.get("controls", [])
            if c.get("observable") is True and type(c.get("node")) is int and c["node"] not in offered_nodes
            and isinstance(c.get("role"), str) and isinstance(c.get("label"), str)
        ][:16]
        self.agent.execution_context = {
            "objective": self.goal, "step": asdict(step) if step else None, "step_index": self.index,
            "final_checks": [asdict(c) for c in self.checks], "verification": self.verification,
            "check_evidence": self.check_evidence, "additional_verified": self.additional_verified,
            "verified_control_progress": [asdict(c) for c in sorted(self.completed_checks, key=repr)],
            "deferred_steps": sorted(self.deferred_steps), "feedback": self.feedback,
            "unsupported_controls": unsupported_controls,
            "_last_mutation": self.last_mutation, "_forbidden_targets": tuple(self.forbidden_targets),
            **({"late_bindings": True} if self.late_bindings else {}),
        }
        marker = page.get("semantic_marker")
        source_digest = (hashlib.sha256(json.dumps(marker, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                         if marker is not None else None)
        self.agent.decision_context = {
            "_source_fingerprint": page.get("fingerprint"), "_source_semantic_digest": source_digest,
            "verification": list(self.verification) if self.verification is not None else None,
            "check_evidence_states": ([r["state"] for r in self.check_evidence]
                                      if self.check_evidence is not None else None),
            "completed_checks_count": len(self.completed_checks), "index": self.index,
            "last_mutation_present": self.last_mutation is not None,
        }
        self.verification = None
        before_count = len(self.agent.state["history"])
        self.pending_binding = None
        try:
            self._phase = "prediction"
            self.agent.command("predict")
            if time.perf_counter() - self.started >= self.max_seconds:
                self.agent.state["decision"] = None
                self.observe()
                if self.status != "done":
                    self.limit_stop()
                return
            state = self.agent.state
            if state["decision"] and state["status"] == "predicted":
                if self.repeated_mutation():
                    raise RepeatedMutation("An unchanged mutation is not safe to replay.")
                page = state["page"]
                action = next((a for a in page["actions"] if a["id"] == state["decision"]["choice"]), {})
                self._phase = "execution"
                self.agent.command("act", {"fingerprint": page["fingerprint"]})
                if self.pending_binding is not None and not (
                        len(state["history"]) > before_count and state["attempts"][-1]["status"] == "executed" and
                        state["attempts"][-1].get("receipt", {}).get("status", "executed") == "executed"):
                    self.status, self.stop_reason = "needs_attention", "late_binding_unacknowledged"
                    return
                if action.get("kind") in {"click", "fill", "select", "press_key", "set_range"}:
                    key = (_mutation_key(action) if action["kind"] != "set_range" else
                           ("set_range", action["node"], state["history"][-1].get("text")))
                    self.last_mutation = (
                        key, page.get("semantic_marker", page.get("marker", page.get("fingerprint"))),
                        state["page"].get(
                            "semantic_marker", state["page"].get("marker", state["page"].get("fingerprint"))))
        except PolicyRejected:
            if len(self.agent.state["history"]) != before_count:
                raise  # An issued input is never a recoverable policy denial.
            if self.pending_binding is not None:
                self.status, self.stop_reason = "needs_attention", "late_binding_rejected"
                return  # A reserved binding cannot move to another recipient.
            page = self.agent.state["page"]
            marker = page.get("semantic_marker", page.get("marker", page.get("fingerprint")))
            self.forbidden_targets.add((repr(marker), *_mutation_key(action)))
            self.observe()  # Fresh checks, no stale settling and no mutation retry.
            if self.status == "ready":
                self.recover("policy_rejected", target=action["label"])
                if self.status == "ready":
                    fresh = self.agent.state["page"]
                    current = repr(fresh.get("semantic_marker", fresh.get("marker", fresh.get("fingerprint"))))
                    candidates = (a for a in fresh["actions"] if a["kind"] in
                                  {"click", "fill", "select", "scroll_to", "press_key", "set_range"})
                    if not any((current, *_mutation_key(a)) not in self.forbidden_targets for a in candidates):
                        self.status, self.stop_reason = "abandoned", "policy_exhausted"
                        self.events.append({"kind": "abandoned", "reason": "policy_rejected",
                                            "scope": "No unblocked observed target remains"})
            return
        except StalePage:
            if len(self.agent.state["history"]) != before_count:
                raise  # Execution was logged; a failed result read cannot authorize a retry.
            if self.pending_binding is not None:
                self.status, self.stop_reason = "needs_attention", "late_binding_rejected"
                return
            self.agent.state.update(status="ready", decision=None)
            self.agent.state["stale_retries"] += 1
            settled = self.settle_after_stale(self.agent.state["page"])
            if self.status == "ready" and self.index == step_index and not settled:
                self.stale_failures += 1
                self.feedback = {"reason": "stale_target", "instruction":
                                 "No input occurred. Choose from the new actionable table; never replay the decision."}
                self.events.append({"kind": "observation_correction", "attempt": self.stale_failures})
                if self.stale_failures > self.local_attempts:
                    self.status, self.stop_reason = "abandoned", "unstable_observation"
                    self.events.append({"kind": "abandoned", "reason": "stale_target",
                                        "scope": "Input boundary did not settle; no semantic replan or mutation retry"})
            else:
                self.stale_failures = 0
            return
        except (PreparedTextUnavailable, RepeatedMutation) as exc:
            self.observe()
            if self.status == "ready":
                reason = "missing_prepared_text" if isinstance(exc, PreparedTextUnavailable) else "repeated_mutation"
                self.recover(reason)
            return
        self.stale_failures = 0
        self.observe()
        if self.pending_binding is not None:
            # A second fresh read rejects transient acknowledgment before committing an owned slot.
            self.observe()
            owner = self.pending_binding
            page = self.agent.state["page"]
            target = owner["target"]
            bound = Check("value", owner["source"].value, target.label, target.role, target.group)
            matches = controls(page, bound, facts=True)
            if (page.get("document_id") != owner["document"] or len(matches) != 1 or
                    owner["node"] not in matches or matches[owner["node"]].get("observable") is not True or
                    evidence(page, (bound,))[0]["state"] != "met"):
                self.status, self.stop_reason = "needs_attention", "late_binding_unverified"
                return
            self.value_owners[owner["slot"]] = owner
            self.pending_binding = None
            self.verify_observation(page)
        if self.status == "ready" and self.agent.state["status"] in {"done", "blocked"}:
            if self.index > step_index:
                self.agent.state.update(status="ready", stop_reason=None, decision=None)
            elif self.agent.state["status"] == "done" and self.defer_milestone("unverified_done"):
                pass
            elif (self.agent.state["status"] == "blocked" and self.plan and
                  self.index < len(self.plan.steps) - 1 and self.defer_milestone("model_blocked")):
                pass
            else:
                if self.agent.state["status"] == "done" and self.settle_objective():
                    return
                self.recover("unverified_done" if self.agent.state["status"] == "done" else
                             self.agent.state.get("stop_reason", "model_blocked"))

    def command(self, name="tick"):
        if name != "tick" or self.status != "ready":
            raise ValueError("Only tick is supported on an active objective")
        try:
            self._tick()
        except (KeyboardInterrupt, SystemExit) as exc:
            self.verification = self.additional_verified = None
            self.status, self.stop_reason = "needs_attention", "interrupted"
            self.events.append({"kind": "interrupted", "error": type(exc).__name__, "phase": self._phase})
            raise
        except Exception as exc:
            self.verification = self.additional_verified = None
            self.status, self.stop_reason = "needs_attention", "execution_error"
            event = {"kind": "execution_error", "error": type(exc).__name__, "phase": self._phase}
            if isinstance(exc, PredictionError):
                event["diagnostic"] = deepcopy(exc.diagnostic)
            self.events.append(event)
        return self.snapshot()

    def run(self):
        while self.status == "ready":
            yield self.command()

    def close(self):
        if self.status == "ready":
            self.status, self.stop_reason = "closed", "closed"
        self.agent.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_args):
        if (exc_type is not None or self.status == "needs_attention"
                or self.agent.state["status"] == "needs_attention"
                or getattr(self.browser, "_uncertain_receipt", None) is not None):
            self.agent.discard_text()
            return  # Preserve the owned tab after unavailable/uncertain execution or escaped errors.
        self.close()
