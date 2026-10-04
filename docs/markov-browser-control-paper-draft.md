# Outcome-Checked Browser Control with Prepared Plans and Dynamic Action Candidates

**Paper-development draft — preliminary engineering study, not a completed research paper**
**System:** Jev Ultrafast, `ObjectiveAgent` and declarative policy execution
**Evidence baseline:** commits `f6f9251` and `3aa3478`, followed by the separately tested, uncommitted native-focus guard port
**Historical scope:** modal-dismissal classification and cross-view prepared-value binding were unfinished at draft time. P1–P8 later passed offline checks; see [architecture improvements](architecture-improvement-proposals.md). No new live-site success is established.

> **Central research question.** Can a single upfront preparation stage, followed by a compact operation/target policy and independent outcome verification, provide useful browser control without repeated task-level language-model planning?
>
> **Epistemic boundary.** The design is MDP-inspired, but browser state is only partially observed. We have not established that the implemented state representation satisfies the Markov property, trained a reinforcement-learning policy, proved general safety, or demonstrated broad website reliability.

## Abstract

Browser automation agents must choose actions in changing interfaces while distinguishing a valid interaction from actual task completion. We describe a constrained, closed-loop architecture that separates upfront task preparation, runtime action selection, native browser execution, and caller-owned verification. A natural-language objective is converted into a data-only plan containing advisory milestones, prepared text values, and intermediate checks. At runtime, a DOM-derived observation provides a finite, indexed set of currently supported operations and targets. One Jev/TypeSafe request supplies an operation head and operation-specific target heads; only the target head associated with the selected operation is consumed. Text insertion uses validated prepared values rather than a per-action text-generation model. The evaluated policy configuration allows zero runtime replans. Freshness guards, observed-node execution, phase-specific input receipts, and independent final predicates constrain execution and completion claims.

We formulate this system as a history-dependent controller for a partially observable, goal-conditioned decision process. An augmented observation is a candidate decision state, not an established Markov-sufficient state or a computed Bayesian belief state. Deterministic offline fixtures establish controller contracts but do not evaluate learned-model reasoning. One post-fix live Google Flights search achieved its independently checked outcome in 17.283 seconds including setup and planning, with one logical upfront plan, 15 Jev decisions, and 11 acknowledged actions. A subsequent three-site suite verified zero of three full goals: Booking.com was confounded by an overblocking test guard, Amazon.de exposed unsupported price-range control and incomplete filtering, and Google Maps exposed brittle prepared-value bindings across view changes. These observations identify research questions and failure mechanisms; they do not establish reliability, superiority, or causal effects. We present an experimental program to test state sufficiency, constraint soundness, binding robustness, and the cost–success trade-off under controlled conditions.

**Keywords:** browser agents; Markov decision processes; partial observability; finite-memory control; constrained action spaces; hierarchical planning; outcome verification; execution uncertainty.

## 1. Motivation and scope

The practical problem is not simply to predict a plausible next click. It is to execute a user objective through an interface that may change between observation and input, while retaining a defensible account of what was attempted, what was acknowledged, and what was actually verified.

Throughout this draft, **verified** means that the specified predicates passed on the recorded evidence—not formal verification of the controller, proof of arbitrary website safety, or independently audited backend completion.

The project investigates a small control loop:

```text
natural-language objective
        ↓
one upfront, validated data-only plan
        ↓
fresh browser observation → indexed supported elements
        ↓
one request: operation + operation-specific target heads
        ↓
validate and execute one operation on an observed target
        ↓
fresh caller verification → continue, complete, or stop for inspection
```

The intended division of labor is deliberate:

- A planner prepares task context and values once; it does not prescribe executable site scripts.
- Jev chooses runtime operations and currently observed targets.
- Deterministic code defines supported capabilities, checks freshness, binds values, and executes input.
- Caller-owned predicates, not model self-assessment, decide whether the requested outcome is verified.

The controller/executor are intended to be site-generic, but domain readers and safety guards may be site-specific, caller-owned code. The complete evaluation setup is therefore not claimed to require zero engineering for each new task.

The present scope is reviewed, non-secret, search/navigation work in an isolated browser. Signing in, booking, purchasing, cart changes, and CAPTCHA bypass were prohibited in the reported complex-site suite. This is not a general-purpose transactional agent or a proof that arbitrary web content is safe to process.

### 1.1 Candidate contributions

The following are engineering contributions or hypotheses to evaluate, not established novelty claims:

1. **Runtime specialization:** replace repeated task-level replanning with one preparation stage and a finite operation/target interface.
2. **Grounded dynamic action spaces:** derive executable choices from current observed elements rather than accepting generated selectors, code, or arbitrary navigation URLs.
3. **Separate execution and outcome evidence:** retain acknowledgments and uncertainty independently of caller-verified goal satisfaction.
4. **Explicit failure semantics:** distinguish safe rejection, unverified abandonment, unavailable observation, and uncertain input.
5. **A state-sufficiency research program:** test which observation and memory components are necessary for useful near-Markov control.

A full paper must compare these choices with existing browser-agent systems before asserting novelty.

## 2. Problem formulation: MDP idealization and partial observability

### 2.1 The environment state is larger than the page snapshot

Let \(g\) denote a fixed objective. Let \(x_t\) denote the complete environment state at decision time \(t\). In principle, it includes browser documents, application state, focus, live node identities, pending requests, timers, server/session state, relevant external processes, and execution status.

An idealized goal-conditioned MDP is

\[
\mathcal M_g=(\mathcal X,\mathcal A,P,R_g,\gamma),
\]

with Markov transition condition

\[
P(x_{t+1},r_t\mid x_{0:t},a_{0:t},g)
=P(x_{t+1},r_t\mid x_t,a_t,g).
\]

The implementation does not observe \(x_t\). It receives a projection \(o_t\): visible text, common HTML/ARIA roles and names, ordinary control values/states, actionable elements, modal information, and document/observation identifiers. This is a DOM-derived accessibility-style representation, not a complete browser accessibility tree.

Two browser situations may share a similar visible projection yet behave differently because of hidden loading, overlays, focus, asynchronous callbacks, or server state. Treating a page snapshot alone as a fully observed MDP state would therefore be unjustified.

### 2.2 POMDP interpretation

A more appropriate formulation is

\[
\mathcal P_g=(\mathcal X,\mathcal A,P,\mathcal O,\Omega,R_g,\gamma),
\]

where \(\Omega(o\mid x)\) is the observation model. The interaction history is

\[
H_t=(o_0,a_0,\ldots,a_{t-1},o_t).
\]

A POMDP solver might maintain a belief \(b_t(x)=P(x_t=x\mid H_t)\). The current system does **not** implement that belief update. Instead, it constructs a compact, history-dependent controller representation:

\[
z_t=\phi(o_t,\Pi,k_t,v_t,d_t,h_t,\ell_t,B_t),
\]

where:

- \(\Pi\): accepted plan and immutable prepared values;
- \(k_t\): preferred milestone cursor;
- \(v_t\): current caller-check evidence;
- \(d_t\): verified/deferred milestone information;
- \(h_t\): bounded recent action history and preserved progress;
- \(\ell_t\): relevant execution memory, including the last acknowledged unchanged mutation and uncertainty state;
- \(B_t\): remaining decision, action, correction, and time budgets.

Some components are model-visible; others remain code-owned. For example, the internal unchanged-mutation tuple is used to filter candidates but removed from model-visible execution context.

The process is best described as a **history-dependent closed-loop controller over a partially observed environment**. The chooser receives at most ten recent history entries; planning context uses at most five. The evaluated runs also bound decisions and acknowledged actions at 24. These local bounds do not establish a fixed finite global state space or a formal bound on every log, node cache, or observation structure. Adding memory makes the controller's own updates explicit; it does not prove the resulting representation makes the external environment Markov.

### 2.3 What would establish approximate state sufficiency?

A useful empirical question is whether omitted history improves prediction of subsequent observations, verification changes, or execution failures after conditioning on \(z_t\) and \(a_t\):

\[
P(Y_{t+1}\mid H_t,a_t,g)
\approx P(Y_{t+1}\mid z_t,a_t,g).
\]

Here \(Y_{t+1}\) can include the next observable state, receipt category, check transitions, and action duration. A study could compare predictors using current observations alone, augmented controller state, and longer histories. Remaining predictive gains from history would indicate aliasing in the compact state.

Finite-data prediction tests cannot prove the Markov property. They can provide evidence about the adequacy of a representation within a specified task/environment distribution.

### 2.4 Variable duration and hierarchical structure

Actions, verification windows, and model requests take unequal amounts of time. A latency-sensitive account may therefore use a semi-Markov transition \(P(x',\tau\mid x,a)\), where \(\tau\) is action duration, rather than treating every decision as an equal-duration step.

The upfront plan resembles hierarchical task guidance, but it is not an implemented options-learning algorithm. Milestones are advisory, not hard action sequences. Mechanical typing may require several native input calls; that is one policy-level operation with potentially partial execution, not a generated goal-level macro and not a native atomic transaction.

## 3. System architecture

### 3.1 Preparation and declarative policy

The policy wrapper accepts exactly four fields:

```json
{
  "version": 1,
  "objective": "Find and open the Wikipedia article about Ada Lovelace.",
  "checks": [
    {"kind": "url", "value": "https://en.wikipedia.org/wiki/Ada_Lovelace"},
    {"kind": "title", "value": "Ada Lovelace"}
  ],
  "prepared_plan": null
}
```

This public example uses the existing typed check schema; parsing it does not start a browser or model call. A supplied plan uses the same accepted plan contract; `null` permits one logical upfront planning invocation. URL, credentials, browser handles, budgets, and safety policies are trusted caller/server settings, not additional policy fields.

The forced planning function returns data of the form

\[
\Pi=(g,[(g_i,T_i,C_i)]_{i=1}^{m}),
\]

where \(g_i\) is a milestone, \(T_i\) is prepared text with field-binding metadata, and \(C_i\) is a set of intermediate predicates. Local validation rejects changed objectives, unsupported fields, selectors, executable code, and attempts to replace caller final checks.

The evaluated policy fixes runtime replanning at zero. Explicit replanning remains an option in the underlying controller but is outside these policy runs. An upfront plan is not guaranteed correct, complete, or able to anticipate captions in future views.

### 3.2 Observations, facts, and targets

A browser-side snapshot separates:

- **Control facts:** information used for verification, including disabled or read-only controls.
- **Executable actions:** currently supported targets that may authorize input.

A code-owned WeakMap assigns identities to actual nodes; live references support execution. Replaced elements receive new identities. These are not model-generated selectors or CDP backend-node IDs. Observation-local indices expose a compact finite interface to the model.

During a detected modal, covered background elements are not offered as executable targets. Hidden cached bindings contain no retained value and do not constitute fresh outcome evidence. Observation truncation is reported; excluded candidates cannot be selected.

Screenshots may support diagnostics or recording, but are not consumed by the described decision model.

### 3.3 Dynamic finite action space

For a current representation \(z_t\), let \(\mathcal U(z_t)\) be supported operations, and \(\mathcal T_u(z_t)\) be the currently observed target set for operation \(u\). The executable action space is

\[
\mathcal A(z_t)=
\bigcup_{u\in\mathcal U(z_t)}\{(u,j,\alpha):j\in\mathcal T_u(z_t)\},
\]

Finiteness applies to each observed candidate set, not a fixed universal action vocabulary or complete coverage of all possible browser interactions.

Here \(\alpha\) is a code-owned argument when needed: a uniquely bound prepared text value or an observed dropdown option. Unsupported argument types are not invented at runtime.

The implemented interface includes clicking, text entry into supported editable fields, native selection, revealing observed offscreen elements, scrolling, and waiting. `DONE` and `BLOCKED` are control signals, not browser mutations or proofs of completion/impossibility.

One TypeSafe request asks for an operation choice and operation-specific target choices. At the interface level, this can be represented as

\[
\pi(u,j\mid z,g)=\pi_{\mathrm{op}}(u\mid z,g)
\pi_{\mathrm{target},u}(j\mid z,g).
\]

This is a description of the head-selection interface, not a claim about the provider's internal probabilistic model or calibration. Target questions state the operation they assume; they do not read the operation answer. Only the selected operation's target head is validated and consumed. Unused heads cannot authorize actions or cause irrelevant validation failures.

Returned choices, probability keys/mass, and confidence values undergo local validation. A non-maximal chosen answer is rejected rather than silently replaced by argmax. Valid provider outputs can still select strategically wrong actions.

### 3.4 Prepared values and binding

In the evaluated `ObjectiveAgent` path, `TYPE_TEXT` obtains its value from the accepted plan. It does not invoke the legacy per-action text model.

Prepared values must bind uniquely to observed fields. Exact binding normalizes whitespace in control/group captions, not case, punctuation, accents, roles, values, or URLs. Ambiguity and conflicts fail closed. A unique compatible binding elsewhere in the accepted plan may resolve a current UI blocker; the preferred milestone is not an action lock.

There is also an explicit, default-off, tightly scoped edit-distance recovery option. It requires role/group constraints and strong uniqueness conditions; it does not make final verification fuzzy or establish semantic equivalence.

The Maps failure motivates a different problem: a value prepared for an initial search caption must later be associated with a directions field. Such a transition is not necessarily a typo. A safe general solution must resolve field identity and value ownership without adding site aliases or generating new runtime text. Section 9 describes the unfinished prototype and its requirements.

### 3.5 Freshness and native execution

Before input, the executor checks document and relevant semantic state, supported operation/target correspondence, current geometry, visibility, enabled state, and occlusion. Click/select freshness is scoped to relevant semantic context; text entry and terminal choices use broader comparisons. Scoped freshness is a practical heuristic, not proof that every ignored change is irrelevant.

Native typing additionally requires literal-boolean proof that the original observed field remains connected, focused, visible, and writable after activation and immediately before insertion. Failure stops remaining keyboard/text calls. The executor does not automatically refocus or replay a mutation.

The execution ledger separates attempted, acknowledged, rejected-before-input, and uncertain input. Execution is recorded before observing its result, so a failed result read cannot authorize replay. If an input outcome is unknown, further input in that run is locked while inspection remains possible.

These are tested operational safeguards under a trusted observer/driver, not a proof of global exactly-once semantics. Multiple native calls, web callbacks, timing races, and external side effects remain relevant.

### 3.6 Unchanged-mutation candidate filtering

A generic modal correction introduced two changes:

1. Forward the observed `modal_open` state to the chooser.
2. Exclude click/select candidates matching the last acknowledged unchanged operation, node, value, and current semantic marker.

Indices and values remain those of the observation. Replaced nodes or changed context remain eligible. An execution-time no-replay guard remains a backstop. The system does not force a particular confirmation caption or substitute an unselected action.

This illustrates why memory is necessary: a visible control may look valid but represent an unchanged action that has already been acknowledged. Geometry alone should not create false progress.

## 4. Verification, progress, and termination

Let \(F_g=(f_1,\ldots,f_n)\) be caller-owned final predicates supplied before execution. A fresh snapshot yields ternary evidence

\[
e_i(o_t)\in\{\text{met},\text{unmet},\text{unknown}\}.
\]

Missing, obscured, ambiguous, or unreadable controls are unknown, not successful and not necessarily evidence that a previously entered value disappeared. Supported check kinds include URL, URL containment, title/text containment, control value, and boolean checked state. Additional domain verification is caller-owned and read-only.

A verified terminal outcome requires

\[
V_g(o_t)=
\left[\bigwedge_{i=1}^{n} e_i(o_t)=\text{met}\right]
\land V_{\mathrm{domain}}(o_t),
\]

where the domain term is required when configured. Predicates should be evaluated from the same fresh snapshot. Caller-supplied readers may use selectors; the model may not emit execution selectors or code.

`DONE` is a hypothesis to verify. An acknowledged click is evidence of execution, not of its intended effect. A milestone cursor is not a completion count: unverified intermediate `DONE` may defer a milestone without verifying it. Verified and deferred steps are tracked separately.

The controller distinguishes:

| Outcome | Meaning |
|---|---|
| `done / verified` | Current caller predicates and any configured independent verifier passed. |
| `abandoned` | Goal remained unverified within correction/replanning/global limits; not proof of impossibility. |
| `needs_attention` | Planning/observation/execution evidence was unavailable or input became uncertain; inspect before further work. |

Read-only outcome settling can accommodate asynchronous updates without replaying input. Budget checks prevent further input when a deadline expires during prediction, but in-flight HTTP/browser calls are not forcibly cancelled. The configured wall-clock limit is therefore not a hard real-time guarantee.

## 5. Control algorithm

The following pseudocode summarizes the tested baseline, not the unfinished cross-view extension:

```text
Require unchanged objective g, caller predicates F, trusted browser/safety settings
Accept a validated prepared plan Π, or make one upfront planner invocation
Initialize progress, receipts, correction windows, and budgets

while within configured limits:
    o ← fresh observation
    if observation unavailable: stop for inspection
    if caller and domain predicates all pass: return VERIFIED
    update milestone progress from fresh evidence

    A ← supported observed action/target table
    A ← remove already-forbidden acknowledged unchanged click/select candidates
    (operation, target) ← one Jev request over A and current objective context
    validate only the selected operation's target head

    if signal is DONE or BLOCKED:
        verify freshly; defer or apply bounded local correction when appropriate
        never treat the signal as proof
        continue or stop unverified within the configured limits

    if operation is TYPE_TEXT:
        value ← uniquely compatible immutable prepared binding
        if unavailable/ambiguous: issue no text; apply bounded correction or stop

    if freshness rejects before input:
        re-observe and obtain a new decision; do not replay the old command
        continue within the bounded stale-observation window

    receipt ← execute one supported operation on its observed node
    record execution before reading the result
    if receipt/input outcome is uncertain: lock further input; stop for inspection
```

The distinction between **fresh redecision after no input** and **replaying a possibly executed mutation** is fundamental.

## 6. Reward, constraints, and evaluation semantics

No reinforcement-learning training or learned reward model is implemented in the reported work. Offline trajectory processing summarizes controller behavior; it does not optimize policy parameters.

A possible future research objective is

\[
\max_\pi\ \mathbb E\left[
\mathbf 1\{V_g(o_T)\}
-\lambda_\tau\sum_t\tau_t
-\lambda_c\sum_t c_t
\right],
\]

subject to task-specific action constraints and a bounded uncertainty/risk specification. Here \(c_t\) denotes measured computational or monetary cost, not a value inferred from logical call counts. The present study has not estimated this objective or its constraint violation probabilities.

Safety cannot be reduced to a reward penalty. The design uses code-owned capability restrictions, caller guards, freshness checks, and uncertainty stops. Their soundness and completeness are assumptions to test. The Booking failure shows the cost of an overconservative classifier: a safe dismissal can be rejected because its caption mentions a prohibited action.

The implemented evaluator records redacted trajectories as they are yielded, avoiding the mistaken treatment of cumulative mutable snapshots as independent samples. It distinguishes acknowledged actions from rejected/uncertain attempts. Its terminal score is a controller-reported diagnostic: verified `1`, abandoned `0`, and inconclusive/incomplete `None`. Inconclusive covers records without a usable conclusive outcome, including uncertainty; incomplete covers a trajectory without a terminal record. An expected negative fixture pass means a safety/control assertion held, not that its user goal was reached. This is not independent environmental ground truth. Aggregate verified rate includes all recorded runs in its denominator.

Observed transitions are descriptive projections. We have not estimated a reusable transition kernel, demonstrated state sufficiency, or performed policy/value iteration.

## 7. Existing evidence

### 7.1 Evidence classes and code epochs

Evidence should be separated into four classes:

1. Parser, validator, guard, and ledger tests with simulated boundaries.
2. Owned local-browser fixtures with no model calls.
3. Live-site runs with learned-model decisions and independent DOM/domain checks.
4. Unfinished prototypes and proposed experiments.

They cannot be exchanged as evidence for one another.

| Code/evidence stage | Recorded validation |
|---|---|
| Declarative policy and offline evaluation (`f6f9251`) | 453 offline tests, lint/syntax/build checks. |
| Generic modal context and unchanged-target exclusion (`3aa3478`) | 471 offline tests, lint/syntax/build checks; subsequent successful Flights run. |
| Narrow native-focus guard port, before the current binding edits | 489 offline tests; four owned Chrome focus cases; 24 local outcome-reader/predicate checks. |
| Current dismissal/binding work | Dismissal helper had 27 unit cases pass. A narrower initial binding prototype was tested before subsequent lease changes. Latest binding code is unfinished; no current full-suite validation or live success is claimed. |

The historical test counts refer to different code epochs, not independent performance samples. Local negative focus-case passes mean redirected text was prevented and execution locked, not that a website goal succeeded.

### 7.2 Deterministic policy fixtures

The basic saved fixture report uses two repeats of three scenarios: prepared text/submit, native selection/submit, and missing prepared text. The progressing **stub** policy achieved six of six expected fixture outcomes, including safe failure; four of six goals were verified. `premature_done` and `blocked` stubs achieved zero verified goals. All variants made zero live model calls.

The adversarial saved report contains four runs per variant. The progressing stub achieved four of four expected outcomes: two verified and two inconclusive. The other stubs verified zero goals. These results demonstrate mechanics such as refusal to manufacture success from terminal signals and preservation of uncertainty; they do not demonstrate that Jev learned these behaviors.

Repeated deterministic fixtures do not provide independent estimates of model success probabilities.

### 7.3 Live Flights observations

The search-only objective requested one-way Zürich–London travel on 20 March 2027, one adult, no children/infants, economy, stopping before flight selection or booking.

| Observation | Full goal | Logical plans | Jev decisions | Acknowledged actions | Recorded time |
|---|---:|---:|---:|---:|---:|
| Before the generic modal fix | Unverified | 1 | 15 | 8 | 14.632 s including setup; 13.959 s excluding setup. |
| After modal context + unchanged-target exclusion | Verified | 1 | 15 | 11 | 17.283 s including setup; 16.797 s excluding setup. |

The first attempt stopped on repeated mutation with zero runtime replans. Read-only inspection found an open date modal and a populated departure value; obscured controls were not evidence that values had disappeared.

The post-fix attempt passed all nine domain checks and six caller checks, with three flight options visible. Upfront planning took 7.778 seconds. Its 15 decisions reconcile as ten `CLICK`, three `TYPE_TEXT`, one `WAIT`, and one terminal `DONE`: the ledger records eight executed clicks and three executed fills, two rejected clicks and one rejected wait, and no execution attempt for `DONE`. These categories were checked against the private raw ledger; decision counts are not mutation counts. Three pre-input freshness rejections led to new observations/decisions, not replay. There were zero text-helper calls, runtime replans, uncertain inputs, or browser transport errors.

This is a useful existence observation for one configured task. It is **not** a reliability estimate, a controlled ablation, or evidence that the fix caused success. Both modal changes were introduced together, and the failed run's shorter duration is not a successful-task speed baseline.

### 7.4 Live complex-site suite

The subsequent suite used isolated Google Chrome, one approved attempt per site, zero runtime replans, 24 decision/action limits, a 90-second controller budget, and bounded local correction. Search-only guards and independent fresh domain checks remained active. No automatic reruns were performed.

| Site and requested outcome | Verified | Plans / Jev / acknowledged actions | Time including planning* | Observed first blocker |
|---|---:|---:|---:|---|
| Booking.com: London, 20–22 March 2027, 2 adults, 0 children, 1 room, free cancellation applied | No | 1 / 6 / 1 | 14.227 s | Test guard treated “Dismiss sign in information.” as a sign-in action. |
| Amazon.de: wireless headphones, under €100, 4 Stars & Up applied | No | 1 / 6 / 3 | 9.064 s | Price-range input unsupported; neither filter applied. |
| Google Maps: Zürich HB → ETH Zürich Zentrum, Rämistrasse 101, visible public-transport routes | No | 1 / 6 / 1 | 6.194 s | Prepared labels failed to bind uniquely to the directions-view fields. |

\* These times exclude earlier homepage/browser preflight and are not directly comparable to the Flights total including setup.

Totals were three logical plans, 18 Jev decisions, five acknowledged actions, and zero helpers, runtime replans, or uncertain inputs. All three full goals remained unverified. Optional cookies were declined where applicable; no purchase, booking, cart change, sign-in, or optional-cookie acceptance was executed.

**Booking: evaluation-harness confound.** The model selected a dismissal control, but the private guard matched incidental “sign in” text. Rejections surfaced as stale-input errors and ended with `unstable_observation`; that label does not establish browser instability. This is not evidence that Jev could not dismiss the popup. Hotel search never started. A later destination-caption discrepancy was observed but not exercised.

**Amazon: capability and progress selection.** The query reached visible results. A visible `Maximum price` control was a native `range` input absent from supported action targets. Jev chose `BLOCKED` three times at that preferred milestone although an observed rating-filter link offered partial progress. Visible ratings alone did not prove a filter had been applied; matching a subset of products would not prove the requested filtering operation.

**Maps: representation/binding failure.** The accepted texts named `Search Google Maps` and `Choose destination`, whereas Directions exposed `Choose starting point, or click on the map...` and `Choose destination...`. Three text choices found no unique prepared binding and issued no text. Origin, destination, transit mode, and route options were not verified.

The suite supplies informative failure cases, not a representative benchmark. Booking's confound must be reported separately rather than silently removed from the overall zero-of-three outcome or reclassified as a model capability failure.

### 7.5 Measurement cautions

“Logical invocation” means an instrumented helper call, not a count of HTTP dispatches, retries, tokens, or billed inference. The current evidence does not support dollar-cost comparisons or claims of lower token use than another agent.

The live observations are sequential, hand-selected engineering trials sharing an evolving setup. They are selected policy-phase observations, not an exhaustive registry of all earlier repository demos and diagnostic failures. A paper must register every comparable scheduled trial and its version rather than select favorable examples. They are not randomized independent draws. No success-rate confidence interval, speedup, causal attribution, or cross-site generalization claim is justified from them.

## 8. Failure mechanisms as research findings

### 8.1 Observation aliasing and modal commitment

Visible values alone do not expose whether a modal edit remains uncommitted. Explicit modal context and observed confirmation targets help represent this distinction. An action must still be chosen; code must not automatically click a familiar confirmation caption.

### 8.2 Policy validity versus executability versus goal progress

These are separate tests:

1. Does the model output satisfy the finite-choice contract?
2. Is the observed target currently executable under the guard?
3. Did execution receive an acknowledgment or become uncertain?
4. Does fresh evidence show progress or full goal satisfaction?

Passing an earlier test does not imply passing a later one.

### 8.3 Prepared-value ownership versus field naming

A prepared value can be correct while its label is obsolete. Guessing aliases, relaxing final predicates, or enabling a runtime text model would mask the underlying problem rather than resolve it under the stated design.

Critically, a naive fallback that sends the sole current-step value to any selected new field can misroute values if the old-caption milestone never advances. After filling origin, a subsequent destination choice could receive the origin value. A general binding solution must preserve slot ownership and planner progress together, without transferring authority to caller final checks.

### 8.4 Capability frontier versus premature impossibility claims

An unsupported widget may prevent a complete goal while other supported actions can still make partial progress. `BLOCKED` is not a proof that the objective is impossible. Capability-aware frontier detection, milestone deferral, and progress toward other requirements are separate questions for evaluation.

### 8.5 Safety soundness and overblocking

The dismissal false positive illustrates incomplete safety classification. A broad substring guard can prohibit harmless controls that discuss a forbidden action. Conversely, a broad “starts with dismiss” exemption could admit a coordinated transaction or navigation. A safe test policy requires action-intent discrimination plus unchanged target, host/path, anti-automation, and transaction boundaries.

## 9. Current prototype and proposed extensions

This section describes work in progress, **not completed functionality or new live evidence**.

### 9.1 Passive-dismissal classification

A conservative helper recognizes unique observed modal button clicks with simple close/dismiss captions and rejects links and coordinated action wording. It is a label heuristic, not proof of a website callback's effects. The original failed suite and its runner must remain immutable historical evidence; a future harness candidate needs its own version and tests.

Before reuse, tests must show the safe sign-in-information dismissal is admitted while sign-in, purchase, consent, CAPTCHA, foreign-host, and product/property-selection restrictions remain enforced. Twenty-seven helper cases have passed; integration of the corrected caller guard is not yet established.

### 9.2 Default-off cross-view binding

The development direction is a narrow caller opt-in, outside the closed v1 policy schema:

- Reuse exactly one immutable current-step value, with no additional value-generation call.
- Require a complete, non-modal observation and a unique, readable/writable offered field.
- Preserve exact-match authority and explicit role/group constraints.
- Reserve a plan-step/document/node association before dispatch.
- Never move an unresolved or uncertain binding to another field.
- Use acknowledged execution **and** fresh exact value evidence at that same node before advancing a binding-equivalent planner predicate.
- Leave caller final predicates unchanged; do not rewrite an old final label into a successful new one.
- Stop on lost identity, uncertain input, or unverified effect rather than rebinding/retyping.

The latest lease changes have not received full validation. Source presence and earlier tests of a simpler prototype do not establish this contract. Required regressions include origin-then-destination value separation, failed effect verification with zero subsequent misrouted input, node replacement/navigation, acknowledgment loss, duplicate steps, ambiguous controls, and stale-caption caller checks remaining unverified.

This opt-in deliberately transfers some field-recipient judgment to Jev's observed-target choice. It must not be represented as semantic identity proof or automatically enabled for sensitive forms.

### 9.3 Unsupported range controls

Price sliders remain unsupported in the evaluated action interface. Supporting them requires a code-owned operation contract, observed bounds and value semantics, native execution accounting, and independent effect verification. Nonlinear slider mapping must not be assumed linear from geometry. Adding a site-specific URL filter, hardcoded input sequence, or executor field value would change the problem and invalidate comparisons.

## 10. Experimental program for a full paper

### 10.1 Preregistered questions and hypotheses

| Question | Testable hypothesis | Falsifying evidence |
|---|---|---|
| Does augmented state reduce observation aliasing? | Modal/progress/receipt memory improves held-out transition or failure prediction over observation-only features. | No gain, or substantial residual gains from long history. |
| Does closed runtime control reduce planning burden? | Comparable verified success needs fewer task-level planner invocations than a repeated-planning baseline. | Material success loss or no measured resource benefit. |
| Does unchanged-target exclusion help? | It reduces unchanged acknowledged mutation attempts without reducing useful changed-context actions. | More false exclusions or no effect in controlled modal tasks. |
| Is late binding safe/useful within scope? | It improves renamed-view tasks without value-slot swaps or final-check relaxation. | Misrouting, identity reuse, or spurious caller success. |
| Do independent checks prevent false completion? | Final predicates reduce false-positive success reports relative to accepting model `DONE`. | Remaining false positives under audited ground truth or poor verifier coverage. |
| Can capability-aware control preserve partial progress? | Unsupported preferred milestones do not unnecessarily block supported remaining requirements. | Premature stopping or added actions with no useful progress. |

These hypotheses have not yet been tested as a controlled study.

### 10.2 Task distribution

Start with reproducible local web applications covering:

- Exact/static fields versus renamed fields after view transitions.
- Modal staging versus committed state.
- Autocomplete query versus selected canonical entity.
- Delayed updates, unrelated DOM churn, focus redirection, and node replacement.
- Supported controls versus unsupported sliders or composite widgets.
- Duplicate captions, occlusion, disabled/read-only state, and truncation.
- Initial success, unverified `DONE`, contradictory signals, and unavailable evidence.

Then add a separately reported live-site evaluation with multiple sites and independently specified goals. Public search tasks should be separated from sensitive or transactional tasks; success on the former does not license the latter.

Choose tasks and sample sizes before observing results. Tune on separate development tasks. Hold out sites, interface variants, and templates rather than testing only familiar captions.

### 10.3 Baselines and ablations

Candidate baselines:

1. General language-model planning at each action with the same browser capabilities and final verifier.
2. One upfront plan plus the same runtime decision interface.
3. Prepared plan plus no runtime replanning, the present intended configuration.
4. A supervised/scripted oracle on local fixtures, used as a reachability diagnostic rather than a generic browser-agent competitor.

Ablate modal visibility, unchanged-candidate filtering, progress memory, prepared binding mode, and caller verification independently. Keep model versions, action repertoire, safety restrictions, task goals, budgets, and browser conditions constant wherever possible.

Never remove native safety guards on real websites for ablations. Simulate unsafe effects in owned fixtures. A baseline with different capabilities, weaker verification, or hidden site scripts is not a valid controlled comparison.

### 10.4 Metrics

Primary outcome: independently audited verified goal rate, with **all scheduled trials** reported and explicit categories for harness confounds, infrastructure failures, unsupported capability, unverified abandonment, and uncertainty.

Secondary metrics:

- False-positive completion rate and verifier precision/recall on annotated states.
- Acknowledged actions, attempted mutations, pre-input rejections, and uncertain inputs, separately.
- Slot misrouting and bound-node identity violations.
- Logical plans/decisions/helpers, actual HTTP attempts/retries, token usage, and billed cost, separately.
- End-to-end latency and planning/observation/decision/execution/verification components.
- Partial requirement coverage without promoting it to full success.
- Termination behavior under budgets and async delays.
- Held-out transition/failure prediction from alternative state representations.

Use confidence intervals and task/site-aware analyses appropriate to the sampling design. Prefer paired comparisons on the same task instances, randomized order, and explicit browser/session reset policies. Determine sample size from a prespecified effect target or precision requirement; do not invent an arbitrary trial count after seeing outcomes.

### 10.5 Reproducibility and evidence release

Each trial should retain exact code/harness versions, uncommitted patches if any, model/provider identifiers, settings, browser version, locale, cache/session/reset policy, verified prerequisites, budget settings, and a timebase definition. Repeated trials must disclose provider or website changes.

Separate code contracts, learned-model trials, and simulator fixtures in reports. Log execution before outcome observation. Preserve raw failures; later guard/verifier fixes create new trial versions rather than retroactively converting failures into passes.

Keep credentials and raw sensitive evidence private. Public releases should include redacted trajectories, bounded local fixtures, validated measurement summaries, and hashes linking them to private records where feasible. Redaction must preserve enough structure to audit action and outcome categories.

## 11. Threats to validity and safety limits

- **Small, selected sample:** one successful configured Flights task and a failed three-site suite do not estimate general reliability.
- **Sequential development:** multiple interventions and environment changes prevent causal attribution.
- **Harness confounds:** guards can overblock; readers/checkers can misclassify. Their errors require independent audit.
- **Shared measurement surface:** a caller-owned DOM verifier is independent of the chooser's self-report, but not equivalent to external backend ground truth.
- **Partial observability:** asynchronous remote state, time, and sessions are incompletely represented.
- **Finite action coverage:** ordinary HTML/ARIA support does not cover all frames, shadow roots, canvas, uploads, keyboard workflows, or composite controls.
- **Binding semantics:** unique captions and nodes establish syntactic/operational identity, not the user's intended semantic recipient.
- **Probabilistic opacity:** provider scores are not demonstrated calibration or formal confidence bounds.
- **Non-atomic input:** focus checks and receipts reduce risk but do not constitute global transactional or exactly-once guarantees.
- **Privacy:** server-side API credentials do not prevent ordinary page text and prepared values from reaching model providers. Current interfaces are for reviewed non-secret data, not automatic PII detection/redaction.
- **No optimization evidence:** no learned reward, RL training, policy improvement result, superiority benchmark, or verified optimal control result is present.

## 12. Related work and bibliography plan

A full literature review is still required. It should distinguish the project from:

- Classical MDPs, POMDPs, history-dependent/finite-memory controllers, and state abstraction.
- Hierarchical/semi-Markov control and options, without implying those algorithms are implemented here.
- Web/browser-agent benchmarks, including WebArena, MiniWoB++, and BrowserGym-family environments.
- Structured/tool-constrained model outputs and grounded action selection.
- Runtime safety shields, action constraints, external verifiers, and uncertainty-aware execution.
- Observation compression, accessibility-derived representations, and GUI state aliasing.

Foundational bibliography candidates include Puterman's *Markov Decision Processes* (1994), Kaelbling–Littman–Cassandra's *Planning and Acting in Partially Observable Stochastic Domains* (1998), Sutton–Precup–Singh's temporal-abstraction framework (1999), and Sutton–Barto's *Reinforcement Learning: An Introduction*, second edition (2018). Verify bibliographic metadata and add exact citations before publication. No systematic novelty search has been performed for this draft.

## 13. Conclusion and manuscript readiness

The developed architecture separates task preparation, finite observed action selection, guarded native execution, and outcome proof. Its central idea is not that a browser snapshot is automatically Markov, but that carefully chosen observation and execution memory may support efficient constrained control without repeated task-level planning.

Existing evidence supports specific engineering contracts and one independently checked live search outcome. It also exposes important limits: safety-classifier confounds, unsupported control types, exact-caption brittleness, and premature blocking. The scientific contribution of a full paper must come from a rigorous formulation plus controlled evidence that quantifies these trade-offs, rather than treating demos, fixture passes, or acknowledgments as general task success.

**Next manuscript milestones:** verify the unfinished binding/guard integration; audit verifier coverage; conduct and cite the literature review; preregister a benchmark/ablation protocol; collect adequately powered trials; replace preliminary observations with analyzed results and a reproducible artifact package. No further paid trials are implied or authorized by this document.

## Appendix A. Implementation and evidence map

Paths are relative to the policy worktree. Historical artifacts should be interpreted at their recorded code epoch, not assumed to describe every current uncommitted change.

| Artifact | Role |
|---|---|
| [`../jev_ultrafast/policy.py`](../jev_ultrafast/policy.py) | Closed declarative schema and existing controller arguments. |
| [`../jev_ultrafast/planning.py`](../jev_ultrafast/planning.py) | Immutable plan/check/text records, parser, prepared binding resolution, and ternary evidence. |
| [`../jev_ultrafast/objective.py`](../jev_ultrafast/objective.py) | Objective controller, milestone/verification logic, correction budgets; includes unfinished current binding edits. |
| [`../jev_ultrafast/model.py`](../jev_ultrafast/model.py) | Dynamic operation/target questions, selected-head validation, unchanged-target exclusion. |
| [`../jev_ultrafast/snapshot.js`](../jev_ultrafast/snapshot.js) | DOM-derived facts, targets, and node identities. |
| [`../jev_ultrafast/browser.py`](../jev_ultrafast/browser.py) | Native execution, freshness, receipts, focus guards, and uncertainty behavior. |
| [`../jev_ultrafast/evaluation.py`](../jev_ultrafast/evaluation.py) | Redacted trajectory recording and diagnostic summaries. |
| [`declarative-policy-contract.html`](declarative-policy-contract.html) | Declarative contract and limitations. |
| [`policy-fixtures-offline.json`](policy-fixtures-offline.json) | Basic deterministic stub/controller evidence. |
| [`policy-fixtures-adversarial-offline.json`](policy-fixtures-adversarial-offline.json) | Adversarial stub/controller evidence. |
| [`policy-flights-chrome-modal-fixed-measurement.json`](policy-flights-chrome-modal-fixed-measurement.json) | Redacted successful single live Flights measurement. |
| [`complex-sites-policy-measurement.json`](complex-sites-policy-measurement.json) | Redacted failed complex suite and harness confound. |
| [`../scripts/check_focus_binding.py`](../scripts/check_focus_binding.py) | Owned no-model native-focus fixture. |
| [`../README.md`](../README.md) | Public API, guard semantics, evidence limits. |
| [`design.md`](design.md) | Earlier design context; its per-action text-helper description belongs to the legacy path, not the evaluated prepared-value policy. |

Full raw run reports and read-only inspections remain private on the internal SSD. They are not included in this manuscript or the repository.

## Appendix B. Full-paper assembly checklist

- [ ] Confirm authorship, contributions, and appropriate attribution of external Jev/TypeSafe and planning models; do not imply the project trained them.
- [ ] Audit licenses, provider terms, website task permissions, and publishable data/artifact rights.
- [ ] Replace candidate literature entries with verified citations and a systematic related-work comparison.
- [ ] Specify one primary operational formulation and explain what each retained theoretical concept predicts or constrains.
- [ ] Publish exact fixtures, checks, task inclusion rules, reset policies, and the full versioned trial registry.
- [ ] Audit check correctness against independently annotated task outcomes, including deliberate false-positive states.
- [ ] Resolve and verify the unfinished binding work before including it as an evaluated contribution.
- [ ] Collect matched baselines and independently ablated results with planned uncertainty estimates.
- [ ] Instrument actual requests, retries, tokens, cost, and latency components.
- [ ] Include architecture, state/observation, receipt-state-machine, and failure-taxonomy figures; generated diagrams must match code and evidence.
- [ ] Label every figure/table as observed data, simulator assertion, or proposed experiment.
- [ ] Release a redacted artifact package that reproduces controller mechanics without paid APIs or credentials.
