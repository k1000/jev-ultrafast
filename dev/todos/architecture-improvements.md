# Architecture improvements — Markov heuristics & robustness

Proposal: `docs/architecture-improvement-proposals.md`. All P1–P8 were approved and copied into this policy worktree after the original-file SHA256 preflight.

## Plan (dedicated and integrated offline verification complete)

- [x] P1 Three-tier state markers in `snapshot.js`; swap call sites in `objective.py`, `model.py`, `browser.py`; delete `semantic_state()`
- [x] P2 `PolicyRejected` + `forbidden_targets` exclusion + `policy_exhausted` stop reason
- [x] P3 `BLOCKED` on non-final milestone → `defer_milestone`; add `unsupported_controls` to execution context
- [x] P8 Partial-viewport `text` checks → `unknown/partial_text`
- [x] P7 Per-decision `z_t` digest in `evaluation.py` and pre-decision producer in `agent.py`/`objective.py`
- [x] P5 `PRESS_KEY {Escape, Enter}` and caller-prepared `SET_RANGE`
- [x] P6 Overlay-based modal detection + `modal_label`
- [x] P4 Finite-choice value binding with source-slot ownership; delete lease prototype

Each item: offline fixture first (no paid APIs), then Ruff, pytest, and `node --check jev_ultrafast/snapshot.js`. On exFAT, every `uv` command must set `UV_PROJECT_ENVIRONMENT=$HOME/.venvs/jev-ultrafast UV_LINK_MODE=copy`; the dedicated checks used that internal-SSD venv directly.

## Review

- P1–P8 have dedicated passing offline verifier commands recorded in the retained swarm worktree's `.swarm/tasks/`. The final swarm-level integrated chain and committed scope independently passed **665 offline pytest tests**, including 27 tests for the optional caller-owned dialog-dismissal helper. Ruff on explicit Python globs excluding exFAT `._*` sidecars, Node syntax checks for both `snapshot.js` and `static/app.js`, an internal-SSD-env `uv build`, and `git diff --check` passed. Both frozen offline fixture JSON reports changed only by empty `decision_states` fields; their 17 fixture tests pass. No paid API calls or live-site reruns were used.
- P1's scoped fill freshness rejects incomplete/truncated observations rather than trusting a missing competing field; this may reduce liveness on very large forms. P4 ownership keys source step/text slots so identical literal values in different steps remain independently bindable. P7's stable SHA-256 state digest is pseudonymous, not protection against guessing low-entropy states. P8 does not detect content hidden in independently scrolling nested containers.
- P6's initial owned Chrome fixture [failed](../../docs/overlay-modal-local-chrome.json) because direct `python scripts/...` imported stale installed package code. The failed report remains intact; a later [read-only recheck](../../docs/overlay-modal-local-chrome-module-readonly.json) of the **same retained owned target**, with the worktree package verified and zero model calls, passed six overlay checks. No new public-site attempt or live Jev reasoning is established.
- Policy-checkout copies of the six P6 Chrome evidence reports redact only the machine-local absolute `source_hashes` key to `scripts/check_overlay_modal.py`; hash values, outcomes, and counts remain unchanged. The raw historical originals in the retained isolated swarm worktree were not rewritten.
- P5's [first owned Chrome run](../../docs/key-range-local-chrome.json) **failed** at the Enter outcome and its target remains retained; it was never replayed. An explicitly authorized [separate owned diagnostic fixture](../../docs/key-range-local-chrome-diagnostic.json) passed three acknowledged observed actions (Escape, Enter, native range), one form submit, and exact fresh text/range checks with zero model/helper calls; its successful tab was closed. This proves local browser mechanics, not live Jev reasoning, Amazon filter success, or a retroactive pass for the first run. Native ranges without explicit min/max/step fail closed.
- Integrated verification and original-file SHA256 preflight passed before safe copy-back. The copied policy checkout was rechecked independently; no commit, push, or paid API call was made.
