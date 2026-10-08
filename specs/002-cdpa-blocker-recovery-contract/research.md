# Research: CDPA Blocker Recovery Contracts

## Decision 1 — Keep accepted-Send identity recovery inside existing durable owners

**Decision**: Reuse RequestLedger/SendReceipt, the active hop wait, CDPAWorker identity discovery, and the existing operator-authorized accepted-send reconciliation child. Add no second ledger or identity state machine.

**Evidence**: current `_owned_or_block` can prove exact accepted user/page, call `_discover_accepted_conversation_identity` once, then immediately block if the URL is still provisional.

**Rejected**: resend, guessed reopen URL, parallel identity store.

## Decision 2 — Anchor identity settle at the first exact unresolved decision, not Send time

**Decision**: when exact unchanged accepted SENT provenance + accepted user + exact owned page are present and the persistent conversation is still provisional at the point that would otherwise block, persist one start time in the existing hop `wait` and a deadline of `min(start + 5.0s, existing response deadline)`. Never reset it.

**Evidence correction**: the durable incident timeline is approximately:
- Send completed 00:39:14.585Z;
- accepted user/provisional page observed 00:39:15.878Z;
- unresolved block decision 00:39:49.846Z;
- same page canonicalized 00:39:49.938Z.

The prior draft incorrectly anchored five seconds to accepted `sent_at`; that budget would have expired ~29 seconds before the decision. The observed race to cover is ~92 ms **after the unresolved decision**, while the long pre-decision response activity remains under the existing response-wait budget.

**Rejected**: start at Send/accepted time; unbounded settle; reload/goto.

## Decision 3 — Preserve current explicit operator authorization for reconciliation

**Decision**: unchanged-receipt settle failure ends as BLOCKED `accepted_conversation_identity_unresolved`. Existing `_route_unresolved_accepted_to_plan` remains callable only from explicit operator Route PLAN. No automatic child creation is added.

**Rationale**: current source explicitly checks state/block/hop, unchanged SENT receipt, and control origin=operator. Reusing that owner means preserving its authorization boundary, not silently expanding it.

## Decision 4 — Receipt divergence remains provenance failure

Immutable hop-versus-ledger receipt divergence is not an identity problem. Fail closed before backend/DOM/reopen activity, preserve both records and attempts, and require explicit manual evidence. Never weaken the existing unchanged-receipt precondition merely to reuse reconciliation.

## Decision 5 — Keep workflow home placement explicit and one-time

Task-level `browser_endpoint_id` remains immutable under generic writes. A deliberately unpinned, pristine, role-unbound, pre-Send Team task may use the explicit initial-placement owner once, then becomes pinned. Role execution binding cannot migrate home ownership.

The current dirty `update_initial_placement` change is an implementation candidate only, not accepted by this spec cycle.

## Decision 6 — Use one lossless ingress canonicalizer

All ingress sources produce Unicode text. File/stdin use strict UTF-8. Canonicalization changes CRLF/lone CR to LF only and preserves everything else. Parser options remain separate. This directly addresses the observed Windows `cdpa.cmd` multiline boundary failure without overfitting quoting rules.

## Decision 7 — Post-acceptance admission corruption uses BLOCKED → explicit Stop → existing replacement

**Decision**:
1. detect and record admission-integrity corruption;
2. preserve accepted request/history and block through the existing worker block owner;
3. require explicit operator Stop;
4. only then call `TaskStore.replace_task_and_rewire` against a STOPPED target;
5. make corrected canonical task/goal fields executable authority;
6. keep retained reports/incident history non-executable.

**Why Stop is explicit**: current replacement owner accepts only STOPPED/BLOCKED targets, but exact-team reuse already requires STOPPED. Accepted malformed work may still have live browser ownership. Explicit Stop provides a legal, auditable terminal boundary before replacement rather than inventing a bypass.

**Compatibility**: the replacement seam must preserve target `workflow_mode`, workflow agents/roles, endpoint/conversation/context policy, dependencies, and Solo goal semantics. Current source does not: it calls `_initial_task_state` without `workflow_mode`/`goal` and defaults to Team.

**Rejected**: Change Goal retroactively; rewrite active prompt; replay corrected Send; parallel replacement subsystem.

## Decision 8 — DOM-bound valid output is terminal authority; completion buttons and LISTEN are not vetoes

Current `role_runtime/response.py::handle` requires copy/feedback/regenerate controls for exact receipts. Current wait snapshot caching can retain a message snapshot from before those controls render. That makes a valid route wait until timeout even when the bound turn is complete.

**Decision**: if the exact accepted user→assistant binding is stable and valid, `stop_visible=false`, and the bound assistant turn is not the current DOM response-activity turn, accept it. Action buttons are corroboration only.

LISTEN can supplement content only for the already-bound assistant. A cached `IS_STREAMING` cannot override a terminal DOM result.

## Decision 9 — Make LISTEN freshness consumable and restart-safe inside existing owners

PageObserver already owns page-lifetime network signals. Extend it with a unique observer-instance id, existing epoch, monotonic signal sequence, and observation time. The existing hop wait/controller evidence owns a last-consumed watermark. Same-sequence observations cannot repeatedly re-arm progress; nonterminal LISTEN older than 5 seconds is diagnostic only; worker/PageObserver recreation invalidates the previous instance until a fresh current-scope signal arrives.

This is not a second state machine; it makes freshness explicit at the existing signal owner. Terminal DOM acceptance does not wait for LISTEN freshness.

## Decision 10 — Terminal arbitration must not depend on signals hidden by the full-snapshot cache

Any signal required to decide terminality must either:
- already be present in the sparse wait probe; or
- cause a full snapshot invalidation.

Because action buttons are no longer terminal gates, late action rendering cannot wedge acceptance. Exact assistant identity/text plus current DOM generation signals remain sufficient.

## Decision 11 — Preserve compact prompts with canonical in-band governing payloads and verified reference

Repository policy explicitly says later turns should not repeat the full Goal or generic bootstrap. B-008/B-010 prove ordinary continuation can lose mandatory constraints; B-013 proves the same rule must cover post-accepted Temporary `execution_recovery` children.

**Decision**: Team create/change-goal surfaces require caller-authored `governing_objective` and explicitly supplied ordered `governing_directives`. Their bounded canonical JSON is the complete authoritative cross-hop task contract. Freeform task/Goal text is turn-1 context only and is never replayed or used as a later-hop fallback. Pre-feature Team tasks without this authoritative payload fail closed before the next substantive Send and recover only through explicit Stop followed by normal new Team task creation.

Explicit objective/directives serialize as canonical compact JSON with fixed key order `objective`,`directives`; semantic SHA-256 covers canonical UTF-8 payload bytes only. Later substantive hops receive three deterministic in-band payloads: semantic payload, compact role continuation contract, and canonical execution-facts/reference JSON. The exact wire framing is length-prefixed and does not require filesystem/connector access.

The reference binds:
- task id and hop id;
- exact canonical absolute `manifest_path` already persisted by TaskStore and proven to be the actual manifest under plans root;
- applicable Goal revision and effective-Goal SHA-256;
- governing semantic payload SHA-256;
- target role;
- continuation-contract SHA-256.

Before Send, CDPA re-resolves durable sources, verifies the canonical manifest identity and all hashes, then emits the verified in-band payloads. The role does not resolve external manifest/tool content; handoff remains supplementary context only.

**Special case**: route/report repair remains a locked non-substantive formatting operation and does not regain the substantive semantic payload. Temporary generation recovery must inherit the accepted parent prompt-authority class and lock fields rather than deriving authority from the generic `execution_recovery` child kind. This avoids accidentally authorizing repeated substantive work during repair.

**Rejected**: repeat any full Goal/constructor on later turns; exact-text `legacy_exact` fallback; infer/summarize governing policy from freeform Goal; treat a non-empty custom continuation as complete without author responsibility; derive recovery authority from `execution_recovery` alone; send multiline governing fields through Windows `%*`; rely only on prior chat context; require role-side manifest/tool lookup; make the handoff report the policy owner.

## Decision 12 — Dependency waits remain derived secondary state

Keep current TaskStore/worker dependency ownership. Projection adds exact parent status/block-code attribution; waiting children consume zero browser slots and never mutate browser state.

## Decision 13 — Cloudflare/CDP/session observations remain environmental evidence

No Cloudflare-specific product state, bypass, spoofing, or cache-clearing recovery is added. Current evidence does not establish causality.

## Evidence Status

- B-001 Windows multiline corruption: VERIFIED from ledger/runtime evidence.
- Accepted provisional→canonical same-page timing: VERIFIED, including the corrected ~34-second accepted-to-decision and ~92-ms decision-to-canonical timeline.
- Existing reconciliation operator gate: VERIFIED by source.
- Existing receipt-divergence fail-closed invariant: VERIFIED by source/prior focused tests.
- Endpoint generic immutability and dirty one-time-placement candidate: VERIFIED by source; implementation acceptance INCOMPLETE.
- Existing replacement target eligibility and Team-default seam: VERIFIED by source.
- B-002/B-007 response action-button gate and wait-cache boundary: VERIFIED by source + durable incident.
- B-008 later-turn Goal/constructor omission: VERIFIED by PromptBuilder and compact-prompt tests; omission is intentional policy optimization whose safety contract is incomplete.
- B-013 accepted Temporary generation-loss recovery without governing semantics: VERIFIED by durable endpoint-9212 evidence plus current `_owned_or_block` / `_prepare_cross_endpoint_recovery` ownership; exactly-once original Send and same-home-endpoint behavior were preserved.
- B-014 bounded full-Goal fallback conflicts with compact-prompt policy: VERIFIED by repository rules/README plus prior independent review; active design now rejects that fallback.
- B-015 hash-valid semantic subset can omit mandatory policy: VERIFIED by prior independent review; active design now makes caller-authored task governing JSON and role continuation contracts complete authoritative cross-hop contracts by construction.
- Dependency owner: VERIFIED by source.
- Runtime implementation of all future fixes: NOT IMPLEMENTED / NOT_MEASURED by design.
