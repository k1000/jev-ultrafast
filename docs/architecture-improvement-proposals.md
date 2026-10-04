# Architecture Improvement Proposals: State Heuristics and Robustness

**Status:** all P1–P8 approved and verified in the isolated swarm worktree. The integrated offline chain passed 665 pytest tests in the full swarm worktree; this committed scope passes 638, excluding 27 tests for an unrelated unfinished safety helper left uncommitted. Ruff on explicit Python globs, both Node syntax checks, internal-SSD-env `uv build`, and `git diff --check` passed. The hash-checked copy-back to the policy worktree is complete; its committed-scope recheck passed 638 offline tests, Ruff, Node syntax checks, and an offline build. The two regenerated offline fixture reports have 17 passing tests. The sections below preserve the original proposals, not claims of live-site success.
**Constraint honored:** no site-specific action scripts, hardcoded executor values, model-emitted selectors, runtime text generation, or paid API calls were added for the verified slices.

Proposals are ordered by expected value ÷ code footprint. Each lists the original evidence, proposed change, risk, and offline verification. Actual implementation differences and evidence are recorded under **Implementation review** below.

---

## P1. One state abstraction, three tiers (root cause behind several "unstable" outcomes)

**Evidence.** The controller currently has *four* state keys that disagree about what counts as "the same state":

| Key | Where | Includes viewport text / scroll? | Used for |
|---|---|---|---|
| `marker` | `snapshot.js:141` | **yes** (6 000 chars of text, `scrollX/Y`) | fill freshness (`browser.py:198`), unchanged-mutation guard (`objective.py:355-358`, `model.py:120-130`), stale settling (`objective.py:429-445`) |
| `fingerprint` | `browser.py:325` | **yes** (`text`, `scroll`) | `page_changed`, legacy `no_progress` (`agent.py:291-296`) |
| `terminal_marker` | `snapshot.js:144` | yes | DONE/BLOCKED freshness |
| `semantic_state()` | `objective.py:385-397` | **no** | blocker key for local-correction budget |

Consequence 1 — **the no-replay guard is defeated by text churn.** `repeated_mutation()` only fires when `marker` before == `marker` after. Any live region in the viewport (countdown, price ticker, carousel caption, "Updated 3 s ago") changes `marker` every observation, so an ineffective click is *never* classified as unchanged and can be repeated until the 24-action budget. The paper's §3.6 claim ("geometry alone should not create false progress") is true, but **text alone currently does create false progress**.

Consequence 2 — **settling never converges on churning pages.** `settle_after_stale` requires two consecutive equal markers 50 ms apart. On a churning page it exhausts its three reads, increments `stale_failures`, and the run ends `unstable_observation` even though nothing relevant moved. This is the exact label Booking ended with; the paper already notes the label "does not establish browser instability".

Consequence 3 — **fills are rejected by irrelevant changes.** `fresh(page)` for `fill` compares the whole marker, so a text tick between decision and input costs a full re-observe + re-decide (the Flights run paid three such rejections). Click/select freshness is already correctly scoped to `page_key + guard(node)`.

**Proposal.** Make the paper's \(z_t\) real by defining three explicit tiers in `snapshot.js` and consuming each in exactly one place:

1. `identity_marker` — what freshness needs: `page_key` + `guard(node)` + `modal_open`. Use it for **fill** as well as click/select (the prepared-value binding depends only on the node's label/role/group, all of which are in `guard`).
2. `semantic_marker` — what progress needs: `(timeOrigin, url without query/fragment, modal_open, controls facts, actions semantics without `node`/`rect`)`. **No text, no scroll.** Use it for `repeated_mutation`, the chooser's unchanged-target exclusion, `settle_after_stale`, and `page_changed`.
3. `terminal_marker` — unchanged.

Then delete `semantic_state()` in favor of `semantic_marker` + the verification tuple, so the controller has one definition of "same state".

**Footprint.** ~15 lines in `snapshot.js`, ~5 call-site swaps. `fingerprint` stays as a debug digest.

**Risk.** A page whose *only* visible effect of an action is a text change (e.g. "3 results found") would now register as "unchanged" for the no-replay guard. That is the desired behavior for the guard — the guard only *excludes the identical click from the next candidate table*; Jev may still choose any other action, and `evidence()` still reads text for `text` checks. Document this explicitly.

**Verify offline.** Fixture page with a `setInterval` clock in the viewport: (a) an ineffective click must be excluded on the second decision; (b) `settle_after_stale` must return `True` within one read; (c) a fill must not be rejected by the tick.

---

## P2. Policy rejection is a first-class signal, not a stale page

**Evidence.** Booking: the caller guard rejected "Dismiss sign in information." before input. The rejection surfaced through `browser.act` → `StalePage` (`browser.py:312-315`), which `_tick` treats as an observation problem (`objective.py:531-551`): it re-settles, bumps `stale_failures`, and abandons as `unstable_observation`. Jev was never told *why* its choice failed and the same target stayed in the next candidate table, so a correct model choice loops into the wrong stop reason.

**Proposal.**
- Introduce `PolicyRejected(ValueError)` in `browser.py`, raised by `validate_action` overrides; receipt status `rejected_by_policy` (distinct from `rejected_before_input`).
- In `_tick`, handle it like `RepeatedMutation`: no settle, no stale counter; set `feedback={"reason": "policy_rejected", "target": label}` and add `(kind, node, value)` to a code-owned `forbidden_targets` set that `model.py:_choose` filters exactly like `_last_mutation` (same mechanism, same marker scope).
- Count it against `local_attempts` via `recover("policy_rejected")` so a guard that forbids everything still terminates, now with an honest stop reason `policy_exhausted`.

**Footprint.** ~25 lines. Reuses the existing exclusion path.

**Risk.** None to safety: the guard still runs before input. The only change is attribution and giving Jev a different candidate table.

**Verify offline.** Stub guard forbidding one of two dismiss buttons: run must execute the other button on decision 2 and never report `unstable_observation`. Existing 489 tests must pass unchanged.

---

## P3. `BLOCKED` defers the milestone before it abandons the objective

**Evidence.** Amazon: Jev chose `BLOCKED` three times at the price milestone although a supported "4 Stars & Up" link existed. `_tick` routes DONE → `defer_milestone` but BLOCKED → `recover` (`objective.py:568-577`), so the cursor never moved and `replanning_exhausted` fired with `max_replans=0`.

**Proposal.** Treat `BLOCKED` on a *non-final* milestone exactly like unverified `DONE`: `defer_milestone("model_blocked")`, then continue. Only when the cursor is already past the last step does `BLOCKED` enter `recover`. Final verification is untouched, so this cannot manufacture success — it only lets the model attempt remaining requirements.

Add one deterministic fact to the execution context: `unsupported_controls` — observed `controls` facts whose node has no entry in `actions` (e.g. `input[type=range]`). Jev can then distinguish "unsupported here" from "impossible", and the evaluator gets a measurable "capability frontier" category (§8.4).

**Footprint.** ~6 lines in `_tick`, ~5 lines for `unsupported_controls` (set difference over existing data).

**Verify offline.** Adversarial stub that returns BLOCKED at step 1 and progresses at step 2: expect `deferred_steps=[0]`, step 2 verified, objective still `abandoned` (final check unmet) — never `verified`.

---

## P4. Finite-choice value binding replaces the lease prototype (Maps)

**Evidence.** `late_binding()` (`objective.py:270-309`) is ~40 lines of guard conditions plus `late_slots`/`pending_late_slot`/`bound_milestone` bookkeeping, still unverified per the paper. The actual decision it makes — *which prepared value belongs in the field Jev just chose* — is a finite choice over at most 16 values, which is exactly the interface the system already trusts TypeSafe with.

**Proposal.** When `TYPE_TEXT` is selected and no exact/normalized binding exists for the node:
1. Build the candidate set `V` = immutable prepared values from the current step **minus values already owned** (see 2), plus `NONE`.
2. **Ownership** is a code-owned map `document_id → {value: node}`, set only on an acknowledged `fill` receipt where the fresh fact at that node shows `value`. An owned value is removed from `V` until its node's value no longer equals it. This is what prevents origin→destination misrouting (§8.3) without leases.
3. Ask one extra TypeSafe `choice` head over `V` ("which supplied value, if any, is this field's recipient?"). `NONE` → `PreparedTextUnavailable` as today. A chosen value is typed; nothing is generated.
4. Milestone grounding: a `value` check whose caption is missing but whose `value` is owned by an observable node with that value is evaluated against the owned node (generalizes `bound_milestone`).

**Footprint.** Deletes `late_binding`, `late_slots`, `pending_late_slot`, `bound_milestone` (~90 lines); adds ~35 (ownership map + one question). Net simpler.

**Risk.** Transfers recipient judgment to the model, as the paper already accepts for the opt-in. Kept opt-in (`late_bindings=True`) and still blocked under `modal_open`, truncated observations, or disabled/readonly fields. Costs one extra head per unbound fill — one request, since TypeSafe answers several questions per call.

**Verify offline.** Fixture with a search view that re-renders into origin/destination fields: fill origin, then assert the destination candidate set excludes the origin value; assert a `NONE` answer types nothing.

---

## P5. Two small code-owned operations: `PRESS_KEY` and `SET_RANGE`

**Evidence.** Booking's blocker was a *dismissal*; Amazon's was a *range input*. Both are generic web idioms, not site features.

- `PRESS_KEY` with a closed key set `{Escape, Enter}`; target = currently observed focused editable field or the modal container. `Escape` is a caption-free, navigation-free dismissal: it removes the need to classify "Dismiss sign in information." at all (the classifier stays as a second path). `Enter` submits a populated search field where no submit button is exposed. Effect is verified by re-observation (`modal_open` flips, URL/path changes), never assumed.
- `SET_RANGE` for `input[type=range]`: snapshot exposes `min/max/step/value`; argument must be a prepared value that parses to a number inside bounds and on the step grid; execution sets `.value` natively and dispatches `input`+`change`. No geometry mapping, so the paper's nonlinear-slider caveat does not apply.

**Footprint.** ~10 lines each in `snapshot.js`, `action_space`, and `browser_operation`.

**Risk.** `Enter` can submit a form — same class of mutation as clicking a submit button, already permitted; guards apply unchanged. `Escape` is the lowest-risk mutation in the repertoire.

---

## P6. Modal detection by hit-tested overlay, with an accessible name

**Evidence.** `snapshot.js:67` only recognizes `dialog[open]` and `[role="dialog"]`. Cookie banners, sign-in nudges and many date pickers are plain fixed `div`s; for those, `modal_open=false` and background controls remain "observable", which is the aliasing the paper calls out in §8.1.

**Proposal.** Treat as modal any visible element with `position: fixed|sticky` whose rect covers ≥ 40 % of the viewport **and** that wins `elementFromPoint` at the viewport center and three quadrant points. Keep the existing role-based path first. Add `modal_label` (accessible name or first heading of the modal, ≤ 80 chars) to the observation; the chooser already forwards `modal_open`, so forwarding the label is a one-key change.

**Verify offline.** Fixture with a fixed-div banner over a form: background `fill` targets must become non-observable; `modal_label` must equal the banner heading.

---

## P7. Record \(z_t\) per decision so §2.3 can actually be run

**Evidence.** The paper's central empirical question (does augmented state reduce aliasing?) needs, per decision, the compact state and the next outcome. `evaluation.py` records redacted trajectories but not a stable state digest.

**Proposal.** Once P1 exists, log per decision: `semantic_marker` hash, `modal_open`, verification tuple, `completed_checks` count, `index`, `last_mutation` present?, receipt category and `check_evidence` transition of the following observation. All already in memory; ~10 lines. The ablation then is a notebook, not new instrumentation.

---

## P8. Truncated-text checks report `unknown`, not `unmet`

**Evidence.** `evidence()` (`planning.py:211`) evaluates `text` checks against viewport text capped at 6 000 chars; a value below the fold yields `unmet`, which the controller treats as contradiction (it discards `completed_checks`). The paper's own rule is "unknown is not a contradiction".

**Proposal.** When `len(page["text"]) >= 6000` or `scroll.y + h < scroll.height`, an unmatched `text` check returns `{"state": "unknown", "reason": "partial_text"}`. Three lines.

---

## Sequencing

```
P1 (tiers)  ──►  P2 (policy signal)  ──►  P7 (log z_t)
     │
     └──►  P3 (BLOCKED defers)   P5 (keys/range)   P6 (overlay modal)   P8 (unknown text)
                        │
                        └──►  P4 (finite-choice binding; after P1 because ownership keys on document/semantic state)
```

P1–P3 and P8 are small, independent of model behavior, and directly address the three recorded failures' *stop reasons*. P4–P6 extend capability and should each get its own owned Chrome fixture before any live trial. No proposal requires a paid call to test.

## What this does **not** propose

- Site aliases, caption dictionaries, or hardcoded sequences (would invalidate the paper's comparisons).
- A learned reward or RL loop — the architecture still has no training signal; P7 only makes the sufficiency *test* possible.
- Loosening final predicates. Every proposal leaves `V_g` untouched.

## Implementation review (verified staging worktree; copy-back pending)

- **P1–P3:** Node-scoped `identity_marker`, text/scroll-independent `semantic_marker`, and terminal freshness have dedicated clock, duplicate-binding, observation-cap, policy-rejection, and nonfinal-`BLOCKED` regressions. Incomplete control/action observations reject fills rather than assuming unique bindings; this is a deliberate safety/liveness tradeoff. Caller final checks remain independent. `PolicyRejected` is distinct from staleness and uncertain input; a guard still runs before every browser mutation. `unsupported_controls` is bounded to 16 sanitized facts.
- **P4:** The original literal-value ownership sketch was unsafe for two steps that intentionally use the same string. The implemented opt-in owner is keyed by **source step/text slot and document/node**, not by literal value; known document replacement or a unique observable readable contradictory value can release it, but unknown document identity cannot. Speculative per-target value-ID/`NONE` heads share the operation/target TypeSafe request; only the selected binding head is validated. Existing lease-prototype failure cases were migrated to the new finite-choice tests, including uncertain receipts, missing/obscured facts, duplicate/ambiguous bindings, and fresh post-fill proof. This is an offline controller/chooser-stub contract, not evidence of improved live Maps performance.
- **P6:** A center-hit ancestor search tests large fixed/sticky overlays without scanning every DOM element; role dialogs take precedence, and a bounded observed `modal_label` is forwarded to Jev. The first [owned Chrome fixture](overlay-modal-local-chrome.json) failed because `python scripts/check_overlay_modal.py` imported stale installed package code. Preserve that failure. A later [read-only check](overlay-modal-local-chrome-module-readonly.json) on the **same retained owned local target**, using worktree imports, passed all six modal/background checks with zero model calls. This verifies local DOM observation, not live model decisions. Smaller banners and irregular or full-screen app shells remain heuristic misses/false positives.
- **P7–P8:** The pre-decision digest is captured from the *actual chooser page after any refresh*; streamed evaluation pairs that pseudonymous SHA-256 digest and whitelisted scalar context with the following receipt/check-evidence transition. An unkeyed digest can still be guessed for low-entropy states. Unmatched viewport `text` checks are `unknown/partial_text` when capped or below-fold content remains; nested independently scrolling containers are not covered. The two fixture-report JSON files were regenerated for the new empty `decision_states` field; their historical outcomes/counts remain unchanged and 17 offline fixture tests pass.
- **P5:** `PRESS_KEY` offers only observed focused Escape or populated search-field Enter (when no actionable submit is exposed), or Escape on an active modal container with focus inside; caller guards still apply. `SET_RANGE` accepts only a uniquely bound current-step prepared numeric value for a native range with explicit min/max/step, exact Decimal bounds/grid, fresh DOM revalidation, and one marked value+input/change mutation. Missing bounds, invalid/ambiguous values, unexpected key/range acknowledgments, and unchanged repeated targets fail closed or stop for inspection. The first [owned Chrome fixture](key-range-local-chrome.json) **failed** after Escape when Enter did not produce an independently verified submit outcome; its target was retained and never replayed. An explicitly authorized [separate owned local diagnostic](key-range-local-chrome-diagnostic.json), after adding carriage-return text to the observed Enter keyDown, passed three acknowledged actions, one Enter keydown/one submit, exact fresh text/range checks, and zero model/helper calls; its successful target was closed. This establishes local browser mechanics with a stub/prepared plan, not live Jev reasoning or Amazon/Booking success. No public Booking/Amazon/Maps retry, paid model call, commit, push, or original-worktree modification is implied by these results. See [the checklist](../dev/todos/architecture-improvements.md).
