<img src="docs/banner.svg" alt="Jev Ultrafast · Browser Use × TypeSafe" width="100%" />

# Jev Ultrafast ⚡

> [!IMPORTANT]
> **The Browser Use Cloud waitlist is open.** Get early access to ultrafast browser agents in the cloud.
> **[Join the waitlist →](https://browser-use.com/ultrafast?utm_source=github&utm_medium=readme&utm_campaign=jev-ultrafast)**

**A browser agent with a dynamic, indexed action space.**

Give it one goal. A larger model prepares milestones, text values, and intermediate checks upfront. [TypeSafe's Jev](https://docs.typesafe.ai/introduction) then chooses operations and observed targets. Independent caller-owned checks determine success. The larger model is not called again during execution by default; bounded last-resort replanning is opt-in.

**Legacy demo: Zürich → London on Google Flights in 7.1 seconds.** One natural-language goal, actual per-field text generation, and loading waits included. This measurement does not include the new upfront planner.

<a href="docs/demo.mp4"><img src="docs/demo.gif" alt="A real Google Flights search at 1× speed, with generated city names and dynamic operation/target decisions" width="100%" /></a>

[Watch the MP4](docs/demo.mp4) · [Measurements](docs/performance.md) · [Read the loop](jev_ultrafast/agent.py)

## The action space

Every observation produces a new element table:

```text
[1] button    Change ticket type · Round trip
[2] combobox  Where from?        · San Francisco
[3] combobox  Where to?          · empty
[4] textbox   Departure          · empty
...
```

The operations are `CLICK`, `TYPE_TEXT`, `SELECT`, `SCROLL_TO`, `SCROLL_UP`, `SCROLL_DOWN`, `PRESS_KEY`, `SET_RANGE`, `WAIT`, `DONE`, and `BLOCKED`. Only supported operations and targets are offered. The bounded offscreen index offers `SCROLL_TO` on observed elements; it scrolls without clicking, then requires a fresh observation and decision before interaction.

```text
                     one TypeSafe request
                    ┌───────────────────────────┐
page → element table → operation                 │
                    │ click_target              │
                    │ type_text_target          │
                    │ select_target, if present │
                    │ scroll_to_target, if any  │
                    └─────────────┬─────────────┘
                        use the matching target
                                  │
                   CLICK [7] ─────┤──→ browser
               TYPE_TEXT [3] ─────┘
                         ↓
               prepared text → browser
```

Target questions are speculative. If the operation is `CLICK`, only `click_target` can execute. Two decisions, **one network round trip**. Each target head contains only compatible elements. Native dropdown choices carry an observed element/option index.

There are no site-specific action scripts or hardcoded executor field strings. Prepared values come from the objective planner, then bind uniquely to observed field labels with optional role/group disambiguation. The legacy `Agent` instead generates text for the selected field with a helper. The Flights example independently verifies its outcome. Screenshots do not drive either loop.

## Try it

```bash
git clone https://github.com/browser-use/jev-ultrafast.git
cd jev-ultrafast
uv sync
cp .env.example .env
# Add TYPESAFE_API_KEY and TEXT_MODEL_API_KEY.
uv run jev
```

Open **http://127.0.0.1:8766** and click **Start demo → Run automatically**. The inspector shows numbered elements, operation probabilities, target probabilities, and executed actions. **Choose next** pauses before execution.

Chrome connects through [Browser Harness](https://github.com/browser-use/browser-harness), installed by `uv sync`. Run `uv run browser-harness --doctor` if it needs connecting. Allow remote debugging in Chrome when prompted.

`TEXT_MODEL_API_KEY` is an OpenRouter key in the example configuration. The current demo uses `inception/mercury-2.5` with reasoning disabled. Gemini, GLM, and DeepSeek can also use the OpenAI-compatible text helper; configure the appropriate model, endpoint, and reasoning setting.

## Prepared-plan execution

[Minimal declarative policy contract](docs/declarative-policy-contract.html) — closed v1 JSON input that reuses the existing controller.

[Scientific paper-development draft](docs/markov-browser-control-paper-draft.md) — MDP/POMDP interpretation, controller architecture, measured evidence, limitations, and a proposed research protocol. Preliminary observations and unfinished prototypes are explicitly distinguished.

```python
from jev_ultrafast import Check, ObjectiveAgent

with ObjectiveAgent(
    "https://en.wikipedia.org/wiki/Main_Page",
    "Find and open the Wikipedia article about Ada Lovelace.",
    checks=(Check("url", "https://en.wikipedia.org/wiki/Ada_Lovelace"),
            Check("title", "Ada Lovelace")),
) as agent:
    for state in agent.run():
        print(state["status"], state["stop_reason"], state["verification"])
```

Configure `PLANNER_API_KEY`, `PLANNER_BASE_URL`, and `PLANNER_MODEL` in the server process. They are separate from the legacy text helper. The Comet skill launcher resolves Pi's Qwen token in memory and uses `qwen3.8-max` for planning; its legacy inspector/smoke modes retain `qwen3.8-flash`. Never copy a Keychain token onto exFAT.

The planner uses a forced `prepare_plan` function whose arguments are `{"objective": "original goal", "plan": [{"goal": "milestone", "texts": [{"label": "field", "value": "text"}], "checks": [{"kind": "value", "label": "field", "value": "text"}]}]}`. The objective must match the caller's goal exactly. Jev receives a preferred milestone plus current actionable elements, history, fresh check evidence, and the original objective. Milestones guide execution, not hard action locks: Jev may resolve a modal or advance other objective requirements. Freshly verified checks advance the cursor; an unverified milestone `DONE` defers that milestone without marking it complete. `verified_steps` and `deferred_steps` are reported separately; `plan_index` is a preferred-goal cursor, not a completion count. An optional `prepared_plan` supplies that same JSON contract directly. Missing or ambiguous values never invoke the old per-action helper.

Only one `prepare_plan` call is accepted. Its JSON arguments are parsed and locally validated against the closed contract; wrong functions, extra calls, changed objectives, truncated output, selectors, and final-check overrides fail before browser input. There is no prose/JSON-content fallback, generated-function dispatch, or second LLM call for a summary. Following [Qwen's function-calling guide](https://docs.qwencloud.com/developer-guides/tool-calling/function-calling), Qwen endpoints use `enable_thinking: false` for forced named functions. Local validation remains authoritative; provider-side strict-schema enforcement is not assumed.

Failed `planner_calls` now include safe `diagnostic` stage/code metadata and elapsed time. Stages distinguish missing/invalid configuration, request construction, transport, function envelope, JSON arguments, and plan validation; validation paths identify positions such as `plan[0].checks[0]` without retaining values. `transport_attempted: false` means the HTTP helper was not invoked; `true` means a logical helper attempt, not proof of an HTTP request or paid inference. Raw responses, arguments, credentials, endpoint URLs, and exception messages are not logged. Existing generic `ValueError` reports cannot be diagnosed retrospectively from timing alone.

TypeSafe prediction failures use `PredictionError(ValueError)` with code-owned diagnostics: configuration/request, transport-helper, envelope, operation validation, or **only the selected operation's target-head validation**. Reasons distinguish missing configuration/answers/model metadata, invalid choices, mismatched probability keys, invalid confidence/probability values, invalid probability mass, and a non-maximal chosen answer. Diagnostics retain only safe stages/reasons, head names, expected-choice counts, elapsed time, and cause types—not raw responses, URLs, values, credentials, or exception messages. Successful `decisions` remain separate from the defensive `prediction_calls` ledger, which also retains failed/interrupted chooser attempts and latency. These are logical invocations: `transport_attempted` means the HTTP helper was called, **not a count of dispatched HTTP requests or paid inference**; existing helper retries may occur. A failed response never authorizes an action or automatically repeats the workflow.

Controller error events now identify observation, caller verification, extra verification, milestone verification, prediction, execution, or controller/context phases and attach typed prediction diagnostics when available. Settling errors also distinguish observation/check/verifier phases; `outcome_settled.failure_phase` locates unavailable verification instead of mislabeling it as browser transport failure. Failure records survive final-evidence errors in the Flights report. This adds diagnostics, not weaker response validation or proof of the historical generic `ValueError`'s cause.

Final checks are supplied **before execution** and remain authoritative. Supported kinds: exact `url`, `url_contains`, case-sensitive `title`/viewport `text` containment, observed-control `value`, and boolean `checked`. Control checks require `label` and optionally `role`/`group`; ambiguous observable matches fail verification. The snapshot separates `controls` (verification facts, including disabled/readonly controls) from `actions` (supported, actionable targets). Covered/background targets are not offered during a modal. `check_evidence` reports **met / unmet / unknown**: missing, obscured, ambiguous, or unreadable controls are unknown, never proof of success. An unmatched viewport `text` check is `unknown/partial_text` when its 6,000-character sample is capped or document content remains below the fold; independently scrolling nested containers are not detected. Hidden cached bindings contain no stored value; historical progress is advisory, not fresh evidence. Facts alone cannot authorize input. All final predicates use the same fresh snapshot. Choose meaningful checks, not a generic phrase that could already be present.

Control-name identity normalizes **whitespace only** in labels and group captions, consistently for prepared values, plan duplicate detection, and final checks. Case, punctuation, accents, roles, expected field values, and URLs are not normalized. Canonical-name collisions remain ambiguous. This fixes presentation discrepancies at the binding contract rather than adding site-specific aliases.

Optional `ObjectiveAgent(..., recover_bindings=True)` permits conservative **prepared-text binding recovery**, never fuzzy final verification. It requires an explicit editable role and matching group, names at least ten characters long, identical digit sequences, a single Levenshtein edit, and a runner-up at distance three or greater. Exact/normalized matches are considered first and never displaced, including ambiguous exact matches. Nonactionable facts still count as competing candidates; reported observation truncation disables recovery. Only the uniquely resolved, currently offered fill node can receive the prepared value, through the unchanged freshness/execution boundary. Conflicting prepared values fail closed. `binding_recovered` events record node/role, edit distance, normalized similarity (`1 - distance / longest-name-length`, **not statistical confidence**), runner-up distance, and source-plan step/text indices—not raw captions, groups, or prepared values. Recovery is disabled by default; it is not a guarantee of semantic identity. Semantic-vector matching is not implemented.

Optional `ObjectiveAgent(..., late_bindings=True)` supports **reviewed, non-secret cross-view inputs** without aliases or runtime text generation. Strict exact/whitespace-normalized caption binding takes precedence, including ambiguity. For an unbound selected `TYPE_TEXT` field, Jev receives speculative finite source-step/text-slot choices plus `NONE` in the **same** TypeSafe operation/target request; only the selected target's binding head is validated. Missing, invalid, or `NONE` IDs type nothing, including when a chooser stub bypasses model validation. A complete non-modal observation, known document identity, unique offered editable witness, matching declared role/group, and the original planner predicate are required. More than eight offered fills fails closed for opt-in choice rather than silently dropping heads. This grants Jev recipient judgment, not proof of semantic correctness or live Maps success.

A selected source slot is reserved to its observed document/node **before input**. Ownership is committed only after an acknowledged fill and fresh same-node value proof; it cannot move on rejected/uncertain input, missing facts, or unknown document identity. Two steps may own the same literal value in different slots. Only a known new document or a unique, readable contradictory value can release an owner. An owned value may ground **only** its matching current-step planner value predicate; caller final checks and accepted plans are never rewritten, and text/URL/title predicates still need original fresh evidence. Stops `late_binding_rejected`, `late_binding_unacknowledged`, and `late_binding_unverified` retain the fail-closed inspection boundary. `binding_late_bound` reports slot/node indices, not raw prepared values. This is a trusted caller option, not a policy-v1 field. Native sliders use the separate exact current-step prepared-value binding described below, not late-binding recipient choice.

The staged architecture changes use three distinct snapshot tiers: a node-scoped `identity_marker` for pre-input freshness, a `semantic_marker` without viewport-text/scroll churn for progress/no-replay/settling, and a separate `terminal_marker` for `DONE`/`BLOCKED` freshness. Fill identity includes competing editable-field facts across roles/groups. A capped control-fact or visible-action table still blocks fill; omitted offscreen reveal actions do not block it when every control fact was retained, since omitted offscreen peers remain in the identity marker. These changes have offline clock, duplicate-field, capped-observation, and policy-stop regressions—not a new live-site reliability measurement.

The [first owned local Chrome overlay fixture](docs/overlay-modal-local-chrome.json) **failed** because direct `python scripts/check_overlay_modal.py` loaded an older installed package instead of this worktree. The failed evidence remains. A later [read-only check on that same retained owned target](docs/overlay-modal-local-chrome-module-readonly.json), using worktree imports, passed six modal/background checks with zero model/helper calls. This verifies hit-tested observation mechanics, not live Jev reasoning or a retry of the historical Booking attempt.

`PRESS_KEY` is a closed `{Escape, Enter}` choice on an observed focused editable field or active modal container. Enter is offered only for a populated search input without an exposed submit control; its caller guard still runs, and a CDP acknowledgment alone is not proof of submission. `SET_RANGE` targets only an observed native `input[type=range]` with explicit min/max/step: `ObjectiveAgent` uniquely binds a current-step prepared numeric value, validates it on the exact Decimal step grid, rechecks the live target/bounds, then dispatches native `input`/`change` in one marked mutation. Missing bounds, ambiguous/missing prepared values, and uncertain acknowledgments do not authorize generated text or automatic replay. The [first owned local Chrome key/range fixture](docs/key-range-local-chrome.json) **failed** at the Enter outcome and its tab remains retained. A separately authorized [new owned diagnostic fixture](docs/key-range-local-chrome-diagnostic.json) passed Escape, Enter, and range with one observed Enter keydown/one form submit, exact fresh text/range checks, and zero model/helper calls; that successful tab was closed. Neither report proves live Jev reasoning or a retroactive Booking/Amazon success.

Caller-owned safety guards can use `jev_ultrafast.safety.is_dialog_dismissal(action, page)` to distinguish an observed passive modal button such as **Dismiss sign in information** from sign-in intent. It is a conservative English-caption heuristic, **not action authorization**: native validation, freshness, scope, navigation and stronger CAPTCHA/transaction/consent prohibitions must still apply. The historical complex-site runner is unchanged; its private offline candidate narrows only the authentication-label exception and has its paid entry point disabled.

For editable autocomplete comboboxes, query text is not proof of a completed selection. If a milestone merely echoes its prepared query, and exactly one caller-owned final value check binds the same unique observed control, an accent-only spelling variant can be grounded to that caller's exact canonical spelling (`Zurich` query → `Zürich` check). Runtime verification remains exact; typing the query alone cannot advance the milestone. No caller canonical value, ambiguous/conflicting bindings, unrelated values, textboxes, and native selects are not guessed or normalized. Prepared text and caller final checks remain unchanged. Grounding is recorded as `milestone_grounded`; accepted initial and replan contracts are preserved in `planner_calls[].plan` before runtime grounding. This is a DOM outcome verifier, not arbitrary semantic or backend proof.

Defaults allow **two local corrections per stable semantic blocker**, **zero runtime replans**, **24 decisions/actions**, and **90 seconds**. Set `max_replans=1` explicitly to enable bounded last-resort planning; without it, an unresolved blocker ends as `abandoned/replanning_exhausted`, not verified success. `BLOCKED` on a non-final milestone defers that milestone without marking it verified; at the final milestone it enters bounded recovery. Repeated unchanged input, missing/conflicting prepared text, or unverified objective-level `DONE` likewise cannot override final checks. Caller-guard denials have a distinct `PolicyRejected`/`rejected_by_policy` receipt, no stale settling or automatic input retry, and bounded `policy_exhausted` abandonment when no safe candidate remains. A changed control/actionability or final-check state starts a new window; DOM IDs, geometry, and unrelated text churn do not. Global budgets still bound every window. Pre-input staleness instead gets at most three extra read-only settling observations, 50 ms apart, followed by a fresh decision—not replay. Repeated unresolved staleness stops as `unstable_observation`, without a semantic replan. Document loading waits without planner calls. Prepared text prefers the current milestone's unique binding; if none matches, a unique, nonconflicting binding elsewhere in the accepted plan can resolve a current UI blocker. Missing/ambiguous values never fall back to generation. Before correcting an unverified **objective-level `DONE`**, the controller allows one read-only outcome window (`settle_timeout=5` seconds by default), capped by the remaining global time budget. The initial state and each new confirmed mutation generation can each receive one window; repeated `DONE`, `WAIT`, or DOM churn cannot repeatedly restart it. Intermediate milestone `DONE` remains advisory and is deferred without this wait. A timeout resumes bounded correction/replanning; unavailable outcome reads stop as `needs_attention/observation_unavailable`, without mutation retries. Set `settle_timeout=0` to disable automatic outcome waiting. Compact current context (up to 3,000 viewport-text characters and five recent actions), check evidence, and recorded progress accompany replanning; planner events report `context_bytes`. The original objective stays unchanged. Browser mutations are never automatically retried. Jev receives observed `modal_open` and bounded `modal_label` context and guidance to commit pending edits using an observed confirmation control, not terminal `DONE`. Native dialogs take precedence over large hit-tested fixed/sticky overlays; the overlay heuristic may miss small banners or misclassify large fixed app shells. Click/select target heads exclude a matching unchanged acknowledged mutation; caller-policy-rejected targets are also excluded only while their semantic marker remains unchanged. Changed context or a replaced node remains eligible, but the caller guard still runs before input. Observed element/option indices and field values are preserved, and the execution-time no-replay guard remains a backstop. This is generic candidate filtering, not automatic confirmation or a site-specific action sequence. Offline picker regressions use three different confirmation captions and blocked browser/planner/helper transports; they test these contracts with a chooser stub, not live Jev reasoning or Google Flights completion. An interrupted mutation or failed post-action observation stops for inspection.

`done/verified` means final checks passed—even if Jev's last signal disagrees. `abandoned` means the objective remains unverified within the configured limits, not that it is impossible. `needs_attention` covers planning failures, unavailable verification (`null`), and execution uncertainty. Limits are checked between stages; in-flight HTTP/browser calls cannot be forcibly cancelled. A deadline that expires during prediction prevents further browser input. Timing includes planning and verification, excluding initial browser setup/navigation.

For CLI use, put caller-owned checks in a JSON array and run `uv run --env-file .env python examples/objective.py --url URL --goal 'GOAL' --checks checks.json`. Use `--prepared-plan plan.json` to supply an existing plan (zero planner calls by default), or let the planner make one upfront call. The CLI defaults to Jev-only runtime decisions; `--max-replans 1` explicitly enables a bounded last-resort call. Tests are offline; configured live runs call paid APIs.

### Declarative policy files

A reviewed, non-secret policy contains exactly `version`, `objective`, `checks`, and `prepared_plan`. Use integer `version: 1`, existing check records, and either the existing prepared-plan JSON contract with the same objective or `null`. Unknown fields and invalid nested records fail before browser setup. The [contract example](docs/declarative-policy-contract.html#example-display) uses public fixture data.

```bash
UV_PROJECT_ENVIRONMENT="$HOME/.venvs/jev-ultrafast" UV_LINK_MODE=copy \
  uv run python examples/objective.py --url 'https://example.org/' --policy policy.json
```

The URL and credentials stay caller/server settings, not policy fields. Do not mix `--policy` with `--goal`, `--checks`, `--prepared-plan`, or `--max-replans` (even zero). Policy execution fixes runtime replanning at zero; a supplied plan makes zero planner/helper calls, while `null` permits at most one logical upfront planner invocation (not a paid HTTP-request count). Jev still chooses each runtime operation/observed target, and caller-owned final checks remain authoritative. Live execution still needs a configured browser and model credentials and can make paid calls; the tests use blocked transports and stubs.

Library callers can use `from jev_ultrafast.policy import parse_policy`, then `ObjectiveAgent(url, **parse_policy(data).agent_kwargs())`. Parsed checks/plans are immutable; each `agent_kwargs()` call returns fresh prepared-plan data. The legacy CLI flags remain supported separately.

This is not a capability registry, YAML interpreter, secret store, or login adapter. Current Jev context includes viewport text, ordinary field values, and prepared/recent text. Keep sensitive pages and secret-bearing policy values out of this interface; it does not automatically detect or redact them.

### Offline policy evaluation

`record_trajectory()` consumes `ObjectiveAgent.run()` **as it yields**, so cumulative mutable snapshots are not mistaken for independent steps. It retains operation/action categories, caller-check status, stop reason, logical call counts, and `decision_states`—a SHA-256 digest of the actual pre-decision semantic marker plus whitelisted scalar context, the following receipt category, and check-evidence transition. It does not retain raw URLs, page text, element labels/IDs, prepared values, or model answers in this evaluation report. The stable digest is **pseudonymous, not anonymous**: low-entropy states may be guessed or linked.

```python
from jev_ultrafast.evaluation import evaluate_trajectories, record_trajectory

with ObjectiveAgent(url, goal, checks=checks, prepared_plan=plan) as agent:
    report = record_trajectory(agent.run())
summary = evaluate_trajectories([report])
```

This processor makes no additional browser/model calls; `agent.run()` still makes its configured Jev calls. `actions` counts acknowledged executions from the attempt ledger, not pre-acknowledgment history entries. `mutation_attempts` includes rejected and uncertain attempts; neither counter proves goal success. An uncertain scroll remains visible as a `SCROLL_TO` decision and an uncertain attempt, with no confirmed transition action. A score of 1 means caller checks were reported verified, 0 means abandoned, and `None` means inconclusive or incomplete. `verified_rate` includes all runs in its denominator. These are **controller-reported** outcomes, not independent external ground truth, a learned reward model, or proof that the compact observations are Markov-sufficient. Use separate trusted checks for real-world success measurements. Offline tests use browser/model stubs.

Run the deterministic fixture comparison without credentials or a browser:

```bash
UV_PROJECT_ENVIRONMENT="$HOME/.venvs/jev-ultrafast" UV_LINK_MODE=copy \
  uv run python -m scripts.evaluate_policy --repeats 2
```

The runner uses simulated text/submit, native SELECT/submit, and missing-prepared-text scenarios. It compares `progress`, `premature_done`, and `blocked` **stub policies**, blocks HTTP/CDP transport, and emits redacted JSON to stdout. `fixture_passes` counts expected safe behavior (including an unverified, mutation-free negative case); it is not the goal-success numerator. The [offline report](docs/policy-fixtures-offline.json) records 6/6 expected outcomes for the progressing stub, with 4/6 verified goals. Each terminal-only stub gets 2/6 expected negative outcomes and zero verified goals. All variants made zero model calls. These results verify fixture/controller mechanics, **not live Jev reasoning, speed, reliability, or an improved learned policy**.

The separate adversarial suite replaces a target before input and simulates an applied input whose acknowledgment is lost:

```bash
UV_PROJECT_ENVIRONMENT="$HOME/.venvs/jev-ultrafast" UV_LINK_MODE=copy \
  uv run python -m scripts.evaluate_policy --adversarial --repeats 2
```

Its [offline report](docs/policy-fixtures-adversarial-offline.json) records 4/4 safety contracts for the progressing stub: two verified stale-target flows and two **inconclusive** uncertain-input flows. Stale rejection requires a fresh decision, never a replay; uncertain input stops after one simulated effect and retains the simulated environment for inspection. Zero confirmed actions is not proof of zero effects: `simulated_inputs` records fixture effects separately from the controller's action ledger. Both terminal-only stubs pass 0/4 adversarial contracts. All model/transport calls remain blocked. This establishes offline recovery mechanics, not real browser transport reliability; the original basic-suite evidence is unchanged.

## Single-shot browser execution

```python
page = browser.observe(screenshot=False)
action = next(a for a in page["actions"] if a["id"] == selected_id)
receipt = browser.execute(action, page, text=prepared_text)  # text only for fill
```

`execute()` accepts only a unique action from the observed table, checks freshness and live target visibility/occlusion, and records an in-memory receipt before any input. Receipts report `executed` (transport acknowledged, **not goal success**), `rejected_before_input`, or `outcome_unknown`. `input_started` means input may have been issued; explicit code-owned target rejection proves otherwise. Missing replies after possible input fail closed.

Each CDP call records its method, phase, duration, and safe error class—no scripts, coordinates, text values, or raw server errors. The receipt identifies target resolution, mouse press/release, selection, scrolling, and typing separately. After an unknown outcome, this instance's `execute()`/`act()` send no further actions: `execute()` returns a defensive copy of the original unresolved receipt, while read-only observation remains available. There is no mutation retry or automatic unlock. Inspect before deciding whether to start a fresh run. This is process-local protection, not browser-side exactly-once delivery or durable crash recovery.

Native fill also requires literal-boolean proof that the original observed field is still connected, focused, visible, and writable after activation and immediately before insertion. Failed/unavailable focus proof stops the remaining keyboard/text calls, records the precise focus phase, and preserves the uncertain-input lock; it never refocuses or retries automatically. `scripts/check_focus_binding.py` runs owned no-model Chrome fixtures covering correct typing, click-driven focus loss, same-caption replacement, and keyboard-driven focus loss. A negative fixture PASS means text was prevented and the lock retained, not that a user goal was achieved.

Malformed native-input acknowledgments are uncertain, not permission to issue the remaining input. Subclasses add safety policy through `validate_action()`, which both `execute()` and `act()` honor; overriding `act()` alone does not protect direct `execute()` callers. The Flights example applies its search-only guard at this shared validation boundary.

Existing `act()` callers use the same boundary: known pre-input rejection raises `StalePage`; unknown input raises `ExecutionUncertain` with its receipt. Agent snapshots expose pre-input `attempts` and `browser_calls`; uncertain execution stops further predictions. Confirmed execution stays in history even when the subsequent observation fails. Read-only settling remains bounded; final outcome verification is still caller-owned.

### Read-only outcome settling

```python
from jev_ultrafast import Check

# After single-shot execute(): an acknowledgment is not the expected UI outcome.
outcome = browser.settle(
    (Check("value", "Ready", label="Status"),),
    timeout=5, interval=0.1,
)
```

`settle()` polls fresh snapshots without models or browser actions. It reports `verified`, `timed_out`, or `unavailable`, plus check evidence, read counts/timing, safe error types, and the last successfully observed page. An optional `verifier(page) -> bool` is trusted, read-only caller/server code evaluated on the **same snapshot**; it adds criteria and cannot replace or weaken the supplied checks. It must never come from model-generated code. Missing/ambiguous facts cannot verify success, and failed final reads cannot reuse earlier met evidence. A complete document alone is not SPA completion.

Results are returned as defensive copies and retained in-memory in `browser.settlements`. Read response timeouts share the remaining budget, including post-input waiting and snapshot reads. IPC connection/setup and synchronous caller verifiers cannot be forcibly preempted, so this is not a hard wall-clock guarantee. Settling never replays input, closes a tab, or unlocks uncertain execution—even if the observed goal is now verified. The raw browser API is opt-in; `ObjectiveAgent` now invokes it before objective-level unverified-DONE recovery as described above. Prefer `ObjectiveAgent(..., verifier=trusted_callback)` for additional outcome criteria. Like the raw settling API, the callback must return a literal boolean and is conjunctive with the immutable caller checks. It receives a defensive copy of the same snapshot, not a second browser read. Snapshots preserve caller `verification`/`check_evidence` separately from `additional_verified` (`null` when absent/unavailable). A true extra result cannot override an unmet caller check. Invalid/failed fresh verification cannot leave prior completion current. Advanced verification extensions may still override `verify_observation(page)` (calling `super()` and preserving the conjunction), not `observe()`: this hook receives the same fresh snapshot in normal observation and settling. A legacy `observe()` override is rejected before browser setup when automatic settling is enabled, to prevent bypassing its verifier; migrate the hook or explicitly disable settling. The Flights example now supplies its full route/date/year/passenger/cabin/results verifier through the generic callback instead of overwriting caller-check results. Context-manager exit retains the owned tab after `needs_attention` or escaped errors; explicit `close()` is still available after inspection. A [local Chrome fixture](docs/browser-settle-local-chrome-final.json) verified a delayed readonly status after eight reads, with exactly one button action and zero model requests. This establishes delayed-outcome mechanics, not live Flights reliability. Reproduce on the existing isolated Chrome setup with `BU_NAME=jev-chrome-diagnostic BU_CDP_URL=http://127.0.0.1:9334 uv run python scripts/check_settle.py --output <new-evidence.json>`; the script refuses to overwrite evidence and retains failed owned tabs.

An additional [final-source local controller fixture](docs/objective-outcome-settling-local-chrome-final.json) completed with **one real action, two Jev stub decisions, seven settling reads, and zero model API requests**. Its base URL criterion was already met; an independent readonly status criterion prevented premature completion until the delayed result arrived. This tests full-verifier integration mechanics only; it does not establish live Jev reasoning or Google Flights success.

## Legacy helper loop

The inspector and older examples retain `Agent` for compatibility and comparable measurements:

```python
from jev_ultrafast import Agent

with Agent(
    "https://www.google.com/travel/flights?hl=en",
    "Find one-way flights from Zurich to London on September 20, 2026, "
    "for one adult in economy. Stop when matching flight options are visible.",
) as agent:
    for state in agent.run():
        print(state["elapsed_ms"], state["status"])
```

Run with `uv run --env-file .env python your_script.py`. The same policy can run a different task:

```bash
uv run --env-file .env python examples/run.py \
  --url https://en.wikipedia.org/wiki/Main_Page \
  --goal 'Find and open the Wikipedia article about Gödel’s incompleteness theorems.'
```

`uv run --env-file .env python examples/flights.py --keep-open` performs the flight search, checks the actual route/date/results, and saves its trace. It does not select or book a flight.

## Why it moves

- **One TypeSafe request per decision cycle.** Operation and target heads share the same observed state. `ObjectiveAgent` consumes prepared text with zero per-action helper calls. In legacy `Agent`, the default text helper runs only for the selected `TYPE_TEXT` field. Opt-in `Agent(..., speculative_text=True)` batches observed editable fields concurrently, but can make unused paid calls.
- **No screenshots in the default agent loop.** Jev consumes structured state. The inspector opts into screenshots; the video uses a separate continuous screencast.
- **One browser call per snapshot.** Read viewport text, visible controls, and up to 48 observed offscreen controls with compact parent-group labels. Keep references to actual DOM nodes; coordinates stay in the browser.
- **Validate the selected target.** Clicks check the document, form values, target, and nearby context. Animation alone does not force another prediction. Resolve current geometry and reject covered controls before input. Offscreen targets only scroll first; a fresh decision must select any subsequent click.
- **Revalidate terminal meaning, not node identity.** `DONE` and `BLOCKED` execute no browser action. Their guard compares captured document identity, URL, viewport, title, viewport text, offered action semantics (including link destinations), and safe form values; replacing otherwise identical DOM nodes does not force another prediction. This is a freshness check on captured evidence, not a full DOM or backend-state verifier. Click and select guards still reject replaced targets. Independent outcome verification remains required.
- **Wait for useful state.** After typing into a combobox, wait for visible suggestions, capped at 200 ms. Other interactions get at most two animation frames or 50 ms. These reads happen after execution is logged.
- **Keep hidden tabs rendering.** Focus emulation prevents background animation throttling without switching Chrome's visible tab.
- **Send visible text.** Offscreen article bodies and footers do not fill the model context.
- **Prepare text before execution.** `ObjectiveAgent` plans values upfront and validates their observed bindings. Legacy `Agent` generates only the selected field after TypeSafe's decision but before browser input. Opt-in speculation prepares values in parallel; the selected field alone can be typed. Reuse a stale retry's result only if the entire helper input and observation are unchanged.

Every executed target is resolved from an observed node. The executor rechecks page freshness and click occlusion. Model output never becomes selectors, coordinates, shell commands, or executable JavaScript. Text-helper output must parse as a small JSON object before typing.

## Diagnosing a stop

The snapshot's `stop_reason` distinguishes `model_blocked` (Jev chose BLOCKED), `no_progress` (the same action repeated three times without a page change), `action_budget`, and `model_budget`. `stale_retries` counts stale-state re-observations; those are not BLOCKED choices. The inspector shows the stop source instead of labeling every stop as an unsupported action.

For open-link goals, the model receives source/result URLs in recent action history. DONE means the goal is already satisfied; BLOCKED requires that it remains unmet. A matched destination still needs independent verification—model BLOCKED is not automatically promoted to DONE. In a targeted HN-to-arXiv diagnostic, Jev chose BLOCKED after reaching the requested article; after clarifying the terminal criteria and adding navigation evidence, three fresh runs chose DONE with independently verified destination URLs/titles. A negative no-dropdown case still chose BLOCKED. These small non-alternating diagnostics are not a general reliability benchmark; see [blocked-navigation-measurement.json](docs/blocked-navigation-measurement.json).

## Small enough to read

| File | Job |
| --- | --- |
| [objective.py](jev_ultrafast/objective.py) | Verified objective controller; runtime replanning opt-in |
| [planning.py](jev_ultrafast/planning.py) | Data-only prepared plans, independent checks, planning client |
| [agent.py](jev_ultrafast/agent.py) | Jev decision/execution loop and legacy text-helper handoff |
| [snapshot.js](jev_ultrafast/snapshot.js) | Atomic DOM snapshot, indexed controls, freshness guards |
| [browser.py](jev_ultrafast/browser.py) | Browser connection, current geometry, execution |
| [model.py](jev_ultrafast/model.py) | Dynamic operation/target heads and text generation |
| [questions.py](jev_ultrafast/questions.py) | Model instructions |
| [demo.py](jev_ultrafast/demo.py) | Local inspector |

## Evidence and limits

A subsequent approved [three-site policy/API suite](docs/complex-sites-policy-measurement.json) completed **0/3 full goals** on isolated Chrome after the generic focus guard was ported and passed 489 offline tests plus four no-model native-focus cases. Booking.com was blocked by the private test harness incorrectly treating **Dismiss sign in information** as a sign-in action; this is a harness confound, not proven Jev incapability or browser instability. Amazon.de reached the requested query results but applied neither the price nor rating filter: the price widget was an unsupported native range input, and Jev chose `BLOCKED` rather than using the still-available rating link for partial progress. Google Maps opened Directions but could not bind its upfront prepared values to the new view's exact field captions. All three stopped unverified: **three logical upfront plans, 18 Jev decisions, five acknowledged actions, zero helpers/replans/uncertain inputs**. Each goal received one attempt; failures and tabs were retained, and no booking, purchase, cart change, sign-in, or optional-cookie acceptance was executed. These limited results do not establish general complex-site reliability, and correcting the harness or bindings later cannot retroactively make the attempts PASS. The subsequent dismiss/late-binding work has only offline regression and saved-observation evidence: the saved Booking dismissal is correctly classified and the saved Maps TYPE_TEXT choice can reserve its unchanged prepared value in opt-in mode, with zero new inputs or model calls. [Offline fix verification](docs/cross-view-bindings-offline.json) records 573 passing regressions and 15 saved-observation checks, not new site success.

**One approved live policy/API Chrome attempt now passed the full Flights goal:** one-way Zürich → London on **March 20, 2027**, one adult in economy, with three flight options visible and no flight selection or booking. The [redacted measurement](docs/policy-flights-chrome-modal-fixed-measurement.json) records all nine independent domain checks and six caller checks passing after the generic modal-context/no-replay candidate fixes. Total time was **17.283 s**, including setup and one **7.778 s** upfront plan; there were **15 logical Jev predictions/decisions, 11 acknowledged actions, zero helpers/replans/uncertain inputs**, and three pre-input freshness rejections requiring new decisions, not input replay. This exercised `parse_policy(JSON).agent_kwargs()` with the trusted search-only Flights fixture, not the policy CLI. Raw evidence/source hashes remain private on the internal SSD; the results tab was retained. This is one successful attempt, not a reliability benchmark or proof of the fix's causal effect. Historical failures and their evidence below remain unchanged; their unproven-success statements describe those earlier stages.

The next single approved [diagnostic Chrome attempt](docs/objective-flights-chrome-stage-diagnostics.json) identified the precise rejection: `prediction / operation_validation / choice_not_maximal`. The returned operation belonged to the offered choice set and passed probability-key, numeric, confidence, and mass checks, but its probability was not maximal; target-head validation was not reached. This identifies the application validation constraint, not the provider contract or the old generic error's proven cause. The run still failed before date selection: 22.043 s including setup, one 16.766 s plan, nine logical prediction attempts/eight successful decisions, six confirmed actions, zero helpers/replans, and 101 browser calls with zero transport errors. The owned tab was retained and no automatic retry occurred. Next investigate whether requiring distribution argmax is warranted; do not silently change the selected operation, weaken observed-target authorization, or treat this as verified completion.

A separately approved [Chrome attempt after the root fixes](docs/objective-flights-chrome-root-fixed.json) stopped as `needs_attention/execution_error`, with a safe `ValueError` event, before setting the departure date. One-way, Zürich, London, one adult/no other passengers, and economy were independently verified; date/year/results were not. It recorded one 17.157 s plan, eight successful Jev decisions, six confirmed actions, zero helpers/replans, and 101 browser calls with zero transport errors. Total including setup was 24.836 s. A departure-opening click was explicitly rejected before input by freshness checks; the later generic error lacks stage metadata, so its source cannot be established from this report. The owned tab was retained, no mutation was replayed, and no automatic paid retry occurred. Thus a full verified live Flights completion remains unproven despite the local contract tests.

The next approved [Chrome attempt](docs/objective-flights-chrome-corrected-verifier.json) achieved all nine domain outcomes but still abandoned: its caller destination binding had a trailing space while the observed label did not. The example's old subclass overwrote the displayed verification array, hiding that missing caller control. The generic whitespace-identity fix and conjunctive verifier callback address these causes without weakening expected values or changing that historical failure to PASS. A [local non-flight Chrome fixture](docs/binding-contract-local-chrome-final.json) verifies the new contracts with two real actions, three Jev stub decisions, delayed extra verification, and zero model/helper requests. Reproduce with `BU_NAME=jev-chrome-diagnostic BU_CDP_URL=http://127.0.0.1:9334 uv run python scripts/check_binding_contract.py --output <new-evidence.json>`. Offline cases cover invoice/project/settings names, selects, checkboxes, readonly facts, scoped typos, collisions, incomplete observations, and strict callback composition. These establish mechanics, not universal matching accuracy or a fresh live Flights PASS.

A [no-model diagnostic](docs/flights-results-readonly-diagnostic.json) reopened the failed Chrome run's recorded results URL once and sampled it over 30 seconds. Visible flight rows used price-prefixed accessibility labels ending with `Select flight`; the old fact reader incorrectly required that marker at the start and instead found hidden selection buttons. The reader now recognizes the selection marker anywhere in the label while preserving visibility, viewport, exact date/year, route, passenger, and cabin requirements. An offline extraction regression covers both label formats plus hidden/offscreen exclusions. [Read-only revalidation on the same retained tab](docs/flights-results-verifier-corrected-readonly.json) passed all nine independent checks with three visible options and zero model/input calls. This establishes a verifier false negative on the diagnostic page, **not a retroactive PASS for the original run or a new end-to-end agent success**. Its historical failure evidence remains unchanged. `scripts/inspect_flights_results.py` performs this bounded, no-model inspection and retains its owned tab; it never replays Search or selects a flight.

The architecture redesign passed **3/3 local Comet controller flows** combining accent-canonical autocomplete, a native modal obscuring background controls, calendar-like subtree replacement, premature milestone `DONE`, native SELECT, submission, exact fresh final checks, and an unchanged unrelated checkbox. Each flow used **one planner stub, nine Jev stub decisions, eight real browser actions, zero helpers, and zero replans**. Both model boundaries and the HTTP helper were stubbed/blocked: this verifies controller/browser mechanics, **not live model reasoning, public Flights completion, or inference-inclusive performance**. Separate evidence: [objective-controller-redesign-final.json](docs/objective-controller-redesign-final.json); reproduce without paid calls using `BU_CDP_URL=http://127.0.0.1:9333 uv run python scripts/check_objective_flow.py`. Full Google Flights success remains unproven. The first approved redesigned full-stack [attempt](docs/objective-flights-redesign-receipts.json) stopped with `BrowserSetupError` before planner/Jev execution. Its exact setup phase was not retained by the example's report; it cannot be inferred retrospectively. Subsequent [read-only inspection](docs/objective-flights-redesign-bootstrap-inspection.json) found successful bridge reads and the same target set as isolated Comet, not a reproduced setup failure. No automatic rerun occurred. Future reports now retain safe setup phase/cause and cleanup/retained-target metadata even when agent construction fails. A separate approved [diagnostic retest](docs/objective-flights-redesign-diagnostic-retest.json) reproduced `document_ready/_IPCResponseTimeout` before model execution: the readiness loop previously aborted on its first five-second IPC timeout. Offline regression tests now cover a transient timeout followed by success, capped remaining-deadline reads, and unchanged single-shot navigation. No post-fix live Flights success is established.

A local Comet `ObjectiveAgent` fixture independently verified the entered value and confirmation with **one Qwen Max function-call plan, two Jev decisions, zero text-helper calls, and zero replans**. Total time was **15.116 s**, including **14.386 s** of upfront planning. Three earlier attempts failed closed before Jev or browser input. This is one successful local fixture, not a speed or reliability benchmark; [objective-measurement.json](docs/objective-measurement.json) preserves failures and the successful plan.

The eight-case public `ObjectiveAgent` smoke suite ran twice: **11/16 passed**, including four negative-case runs that independently confirmed unsupported controls and zero mutations. Two runs failed during browser setup; both native SELECT runs achieved the requested DOM value but failed controller verification; one HN article run remained unverified. Those failures are retained in [web-regression-measurement.json](docs/web-regression-measurement.json). The SELECT verifier now distinguishes DOM values from display labels; a fresh diagnostic passed with one plan, one Jev decision, zero helper calls/replans in **2.981 s**. Its separate [diagnostics](docs/web-regression-diagnostics.json) also record the HN fixture becoming unavailable before planning. This does not make the original suite green or establish reliability. A subsequent offline review fixed error-retention/cleanup bookkeeping and removed the unconditional navigation `safety_verified` claim: navigation runs now report `null`/not assessed, and their PASS means only the requested destination was verified. The historical navigation safety flags are not independent no-mutation evidence; raw reports remain unchanged.

A subsequent two-repeat suite passed **16/16**: **12 verified positive goals** (one upfront plan each, zero replans) and **four independently grounded negative limitations** (zero mutations, one bounded replan each). There were **20 logical planner requests, 30 Jev decisions, and zero helper calls**. Positive-run median was **3.346 s**, including planning and final verification, excluding initial setup/navigation. [web-regression-permalink-measurement.json](docs/web-regression-permalink-measurement.json) records the unchanged destination checks, source URLs, source hashes, counts, and outcomes. The HN article source is now its permanent [story page](https://news.ycombinator.com/item?id=49922437), not the changing front page: this is a different fixture, not a direct before/after reliability or speed comparison.

Browser setup now issues initial navigation once with a 15-second IPC response timeout (Browser Harness's default is five), then waits up to 15 seconds for a complete non-blank document, allowing redirects. Failed bootstrap closes only its owned tab and reports a safe phase/cause plus any cleanup failure. Only read-only readiness checks can be retried. Transient destroyed contexts and IPC read timeouts both retry within the readiness deadline; each read's response timeout is capped at five seconds or the remaining budget, whichever is smaller. Navigation timeouts are never retried. This suite had no startup failures; the historical generic errors lack enough phase evidence to prove their exact cause, so intermittent startup reliability is not established.

A more complex live `ObjectiveAgent` Google Flights attempt requested one-way Zürich → London on March 20, 2027, one adult in economy, stopping before any flight selection. The [test script](scripts/objective_flights.py) uses an atomic, test-owned route/date/year/passenger/cabin/results verifier and a selection guard; it supplies no action plan or executor text values. Caller-owned control checks now expose the canonical route, trip type, displayed date, and cabin to the planner; these are verification criteria, not an execution plan. The full year/passenger/results verifier still determines completion. Optional cookies were [rejected with user approval](docs/flights-consent-setup.json) during separate setup. The measured [attempt](docs/objective-flights-march2027.json) **did not succeed**: one 6.355 s upfront plan, one Jev decision, zero helper calls/replans, zero logged actions, then an IPC timeout and `needs_attention` after 11.935 s. The tab was retained; [read-only inspection](docs/objective-flights-readonly-inspection.json) showed the original round-trip controls unchanged. No input was replayed. Zero logged actions alone is not proof a timed-out browser command had no effect, and this failed attempt does not validate complex-flow reliability.

The existing performance evidence measures legacy `Agent`, not `ObjectiveAgent`. The video predates both the optional speculative text path and the upfront planner. The video is a **7,073 ms** Google Flights run. Timing starts after initial page observation and includes model calls, generated text, browser work, stale decisions, and loading waits. A fresh independent check verifies the one-way setting, Zürich, London, September 20, 2026, and visible flight options. The video plays at 1×, with no opening hold and a 0.5-second final hold.

In six alternating runs with identical models and settings, both versions passed **3/3**. Median task time went from **9.450 s → 7.092 s**, a **25% reduction**; median browser protocol calls went from **1,092 → 101**. This is three repeats of one task on one browser profile, not a general reliability benchmark.

The recorded policy also opened the requested Wikipedia article in **2.798 s** and passed a local hotel search/filter task in **1.896 s**. Runs, failures, source hashes, and measurement boundaries are in [performance.md](docs/performance.md).

In a separate three-pair live comparison on Selenium's public form using Qwen 3.8 Flash, both text modes passed 3/3. Selected-field-only text took a **3.718 s** median with **3 helper requests** total; optional speculation took **5.012 s** with **6 requests**, three unused. For native SELECT, both modes passed 3/3 at **0.660 s** and **0.654 s** medians respectively, but speculation made **6 unnecessary helper requests** versus zero. Initial navigation was excluded; these small, variable trials do not establish a general speed advantage. The raw per-run counts and timings are in [text-policy-measurement.json](docs/text-policy-measurement.json); [benchmark_text.py](scripts/benchmark_text.py) reproduces the paired protocol with paid APIs.

A `DONE` choice still requires independent outcome verification. The DOM reader handles common HTML and ARIA controls, not the full accessible-name specification. Offscreen indexing is bounded and not a full page tree; a target below that limit still needs ordinary scrolling. Shadow roots, frames, canvas, uploads, pop-up tabs, arbitrary nested scrolling, and keyboard widgets beyond the observed Escape/Enter and native range operations remain outside this MVP. Owned tabs share the existing Chrome profile.

A separate bounded [12-run live consent/widget probe](docs/consent-widget-live-probe.json) verified **8/12** caller-owned outcomes: four explicit optional-cookie rejections (IKEA, Amazon.de, Booking.com, The Verge), a public Wikipedia article search, Selenium's native dropdown, and Booking.com's date and occupancy popovers. The four failed runs remain failures: HN navigation stopped with input uncertainty and its owned tab was retained; Amazon search abandoned on an overstrict URL check before a separate read-only inspection saw visible results; Skyscanner's date-picker attempt made no input after staleness/policy rejection; and its Italian necessary-only consent attempt made no input before policy exhaustion. This is a small non-repeated diagnostic, not a reliability estimate. Consent success proves an acknowledged observed choice and fresh dialog dismissal, not backend cookie-policy enforcement. Raw redacted per-run reports remain on the internal SSD; no private session-handoff URLs are filed here. The offscreen-action binding mitigation and live-probe guards have offline regressions; Guardian's live iframe remains unverified, and truncated control facts remain unsupported.

The initial [offline-only iframe-boundary baseline](docs/guardian-iframe-offline.json) detected visible, unindexed modal frames and stopped when no observed Reject was available. A subsequent [owned local OOPIF diagnostic](docs/guardian-iframe-local-diagnostic.json) proved that a parent-session click acknowledgment did **not** click the child, while a child-session click did. The consent probe now opts into **direct-child out-of-process modal button clicks** bound to the observed parent frame owner, active modal, child node identity, and fresh hit testing. Its read-only preflight accepts a uniquely observed Reject inside such a frame; other frames remain unindexed and fail closed. The no-model local fixture verified one synthetic Reject and independent modal dismissal. **No Guardian live consent outcome has been tested or claimed**, and public-site retesting requires a renewed paid-run budget.

The same opt-in now also covers **direct-child same-process (same-site) modal frames**, read through a per-frame isolated world and clicked through the page session at the owner's origin plus the local point; the owner's origin is part of the freshness marker, so a moved frame is a new decision. An [owned-fixture check](docs/inprocess-frame-click-20261004T181404Z.json) clicked exactly once and rejected moved and navigated frames before input; its [first run](docs/inprocess-frame-click-superseded-moved-executed-20261004.json) executed on a moved frame and was fixed. The caller's exact reject-caption list still gates every framed click (`Reject all and subscribe` and `Accept all` are vetoed; BBC's `I do not agree` was added). A read-only preflight on real pages (no input, no model) offered one permitted reject on BBC and none on the Guardian. One approved live consent probe on bbc.com (isolated Chrome, `qwen3.8-flash` planner) used the committed **out-of-process** path: **1 planner call, 1 Jev decision, 1 browser input (`I do not agree`), 0 uncertain inputs**, caller checks and the extra verifier all true, `done/verified`; its private evidence (`bbc-consent-20261004T183003Z.json`) states the scope: one observed rejection, not backend proof. It is a single attempt, not a reliability estimate, and the same-process (in-process) frame path has been exercised live only through a read-only preflight (BBC offered one permitted reject; the Guardian offered none, so it would stop). Beyond that this is offline/fixture evidence: no same-process public-site click was made with this code, and nested frames, transformed or bordered owners are unsupported.

## Development

```bash
uv run ruff check .
uv run pytest
node --check jev_ultrafast/static/app.js
node --check jev_ultrafast/snapshot.js
uv build
```

On this exFAT worktree, set `UV_PROJECT_ENVIRONMENT=$HOME/.venvs/jev-ultrafast UV_LINK_MODE=copy` for every `uv` command; never create `.venv` on T5. Ignore AppleDouble `._*` sidecars when running Ruff (or target explicit Python file globs), without deleting the sidecars. The isolated swarm and committed scope pass **665 offline pytest tests**, including 27 tests for the optional caller-owned dialog-dismissal helper. Ruff on explicit Python globs, both Node syntax checks, internal-SSD-env `uv build`, and `git diff --check` passed; this is not a live-site reliability result. The subsequent consent/widget changes and added offscreen/cap regressions passed **707 offline pytest tests**, Ruff excluding AppleDouble `._*` sidecars, both Node syntax checks, an internal-SSD-env build, and `git diff --check`. Tests are offline. `scripts/regression_web.py` is a paid live `ObjectiveAgent` suite, not a pytest fixture. It requires isolated Comet on port 9333 plus `TYPESAFE_API_KEY` and `PLANNER_API_KEY`, `PLANNER_BASE_URL`, `PLANNER_MODEL` in process memory—no legacy helper credential. Run `uv run --env-file .env python scripts/regression_web.py --repeats 2 --output artifacts/regressions/web.json`. It preflights each fixture, records planning-inclusive execution/verification time, and preserves partial failures incrementally. Negative passes require an unmet goal, independent unsupported-control evidence, unchanged form/page state, and zero mutations; budget exhaustion alone cannot pass. Uncertain input tabs remain open, including exceptions escaping execution before controller status updates, and subsequent repetitions of that case are skipped until inspection. Original execution errors and later evidence/cleanup failures are recorded separately. Form/negative cases verify unchanged other fields and URL; cross-document navigation does not assert that source-page controls were untouched. Never persist a Keychain planner token in `.env` on exFAT.

`uv run python scripts/check_guards.py` checks real controls in a local browser without model calls. Live examples and recording scripts make paid API calls. `scripts/record_flights.py <new-folder>` captures original browser timestamps; `scripts/render_demo.py <recording-folder>` renders that verified run at 1× and crops out the Google account strip. Credentials and raw traces stay ignored.

---

[Browser Use](https://github.com/browser-use/browser-use) · [Browser Harness](https://github.com/browser-use/browser-harness) · [TypeSafe speculative fan-out](https://docs.typesafe.ai/patterns/fan-out)
