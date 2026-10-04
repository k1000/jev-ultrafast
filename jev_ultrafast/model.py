"""TypeSafe makes choices; an optional small OpenAI-compatible model writes field values."""

import json
import math
import os
import time

import httpx

from .questions import NEXT_ACTION, TARGET, TEXT_VALUES

CLIENT = httpx.Client(http2=True, timeout=25)


def post_json(url, key, body):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids, *, head=None):
    stage = "choice_validation" if head is None else (
        "operation_validation" if head == "operation" else "selected_target_validation")
    code = "invalid_answer"
    if isinstance(answer, dict):
        choice, probabilities, confidence = answer.get("choice"), answer.get("probabilities"), answer.get("confidence")
        if not isinstance(choice, str) or choice not in ids:
            code = "invalid_choice"
        elif not isinstance(probabilities, dict):
            code = "invalid_probabilities"
        elif set(probabilities) != set(ids):
            code = "probability_keys_mismatch"
        elif type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
            code = "invalid_confidence"
        elif not all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in probabilities.values()):
            code = "invalid_probability_values"
        elif abs(sum(probabilities.values()) - 1) >= 0.02:
            code = "probability_mass"
        elif probabilities[choice] < max(probabilities.values()) - 1e-6:
            code = "choice_not_maximal"
        else:
            return answer
    metadata = {"expected_choices": len(ids)}
    if head is not None:
        metadata["head"] = head
    raise PredictionError(stage, code, **metadata)


def _mutation_key(action):
    key = tuple(action.get(name) for name in ("kind", "node", "value"))
    return (*key, action.get("key")) if action.get("kind") == "press_key" else key


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT", "scroll_to": "SCROLL_TO",
                  "press_key": "PRESS_KEY", "set_range": "SET_RANGE"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in
                       ("role", "value", "control_value", "checked", "selected", "expanded", "group", "position")
                       if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        if kind == "press_key":
            target = f"{index}:{action['key']}"
        group[target] = action
    return elements, targets, controls


class PredictionError(ValueError):
    """Code-owned stages only; never retain raw responses or exception messages."""

    def __init__(self, stage, code, **metadata):
        self.diagnostic = {"stage": stage, "code": code, **metadata}
        super().__init__(f"Invalid TypeSafe prediction or response; no action executed. ({stage}/{code})")


def choose(state, goal, history, *, execution=None):
    started = time.perf_counter()
    diagnostic = {"stage": "configuration", "code": "missing_configuration", "transport_attempted": False}
    try:
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key or not key.strip():
            raise ValueError()
        diagnostic.update(stage="request", code="invalid_context")
        return _choose(state, goal, history, execution=execution, key=key, diagnostic=diagnostic)
    except PredictionError as exc:
        exc.diagnostic.update(transport_attempted=diagnostic["transport_attempted"],
                              latency_ms=round((time.perf_counter() - started) * 1000))
        raise
    except Exception as exc:
        raise PredictionError(**diagnostic, cause_type=type(exc).__name__,
                              latency_ms=round((time.perf_counter() - started) * 1000)) from None


def _choose(state, goal, history, *, execution, key, diagnostic):
    marker = state.get("semantic_marker", state.get("marker", state.get("fingerprint")))
    last_mutation = execution.get("_last_mutation") if execution is not None else None
    forbidden = execution.get("_forbidden_targets", ()) if execution is not None else ()
    bindings = execution.get("_binding_choices", {}) if execution is not None else {}
    elements, targets, controls = action_space(state["actions"])
    # Policy exclusions and no-replay share the observed semantic state; never rewrite a model choice.
    if last_mutation is not None or forbidden:
        for operation in tuple(targets):
            targets[operation] = {index: a for index, a in targets[operation].items() if
                                  (repr(marker), *_mutation_key(a)) not in forbidden and
                                  (operation not in {"CLICK", "SELECT", "SET_RANGE", "PRESS_KEY"} or
                                   last_mutation is None or marker != last_mutation[1] or
                                   marker != last_mutation[2] or
                                   (_mutation_key(a) != last_mutation[0] and not (
                                    operation == "SET_RANGE" and a.get("kind") == "set_range" and
                                    _mutation_key(a)[:2] == last_mutation[0][:2])))}
            if not targets[operation]:
                del targets[operation]
        eligible = {op: {i.split(":")[0] for i in candidates} for op, candidates in targets.items()}
        for element in elements:
            element["operations"] = [op for op in element["operations"] if element["index"] in eligible.get(op, ())]
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field using a validated supplied value.",
        "SELECT": "Select an observed dropdown value.",
        "SCROLL_TO": "Reveal an observed offscreen element, then re-observe before interacting with it.",
        "PRESS_KEY": "Press only an observed Escape or Enter key on its focused field or active modal.",
        "SET_RANGE": "Set an observed native range control using only its uniquely bound prepared value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(
        DONE="Every requirement is already satisfied by the current page; no further action is needed.",
        BLOCKED="The goal remains unmet and no supported operation can progress.",
    )
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded", "group", "position",
                                           "key", "target_label", "form_label", "min", "max", "step") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET, *([
                "Late binding is enabled: unbound fields have finite supplied source-slot choices or NONE. "
                "Choose TYPE_TEXT only when an observed field is the current milestone's recipient. "
                "Never invent a value or move a reserved binding; code rejects ambiguous or unsafe targets."
            ] if operation == "TYPE_TEXT" and execution and execution.get("late_bindings") is True else [])]},
        }
    # These heads are speculative. Only the selected TYPE_TEXT target's binding may be consumed.
    for index, action in targets.get("TYPE_TEXT", {}).items():
        options = bindings.get(action["id"], {}) if isinstance(bindings, dict) else {}
        if options:
            questions["binding_target_" + index] = {
                "type": "choice", "criteria": {"NONE": "No supplied value belongs to this field",
                                            **{token: {"value": row["value"], "source": row["label"]}
                                               for token, row in options.items()}},
                "instructions": {"goal": goal, "rules": [
                    "Select exactly one supplied source slot for this observed field, or NONE. "
                    "Do not invent text; only the selected field's answer can authorize input."]},
            }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {**{k: state[k] for k in ("url", "title", "text")},
                     **({"modal_open": state["modal_open"]} if "modal_open" in state else {}),
                     **({"modal_label": state["modal_label"]} if "modal_label" in state else {})},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed", "from_url", "url")}
                for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    if execution is not None:
        body["state"]["execution"] = {k: v for k, v in execution.items()
                                      if k not in {"_last_mutation", "_forbidden_targets", "_binding_choices"}}
    started = time.perf_counter()
    diagnostic.update(stage="transport", code="request_failed", transport_attempted=True)
    result = post_json("https://api.typesafe.ai/v1/systemone", key, body)
    diagnostic.update(stage="envelope", code="invalid_answers")
    if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
        raise ValueError()
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations, head="operation")
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    binding_choice = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}),
                                        targets[operation], head=operation.lower() + "_target")
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
        head = "binding_target_" + target
        if operation == "TYPE_TEXT" and head in questions:
            binding_choice = validate_choice(result["answers"].get(head, {}), questions[head]["criteria"],
                                             head=head)["choice"]
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    diagnostic.update(stage="envelope", code="invalid_model")
    if not isinstance(result.get("model"), str) or not result["model"].strip():
        raise ValueError()
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "binding_choice": binding_choice,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def text_context(goal, page, history):
    return {
        "goal": goal,
        "observation": page["fingerprint"],
        "fields": {
            a["id"]: {k: a.get(k) for k in ("label", "role", "value")}
            for a in page["actions"] if a["kind"] == "fill"
        },
        "page": {"url": page["url"], "title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def field_texts(context):
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key:
        raise ValueError("TYPE_TEXT needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    reasoning = {"thinking": {"type": "disabled"}} if "api.deepseek.com/" in base else {"reasoning": {"effort": "low"}}
    if os.environ.get("TEXT_MODEL_REASONING") == "none":
        reasoning = {"reasoning": {"enabled": False}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key,
        {
            "model": model,
            "max_tokens": 1024,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [
                {"role": "system", "content": TEXT_VALUES},
                {
                    "role": "user",
                    "content": json.dumps(context),
                },
            ],
        },
    )
    try:
        output = json.loads(result["choices"][0]["message"]["content"])
        values = output["values"]
        if (set(output) != {"values"} or not isinstance(values, dict)
                or not set(values) <= set(context["fields"])
                or any(v is not None and (not isinstance(v, str) or not v.strip() or len(v) > 2000)
                       for v in values.values())):
            raise ValueError()
    except (ValueError, KeyError, TypeError):
        raise ValueError("Text helper returned no valid field values; nothing typed.") from None
    return {field: value for field, value in values.items() if value is not None}, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }
