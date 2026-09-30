# Research: Runtime Team Execution

## Decision: Runtime-v2 submits only Team Tasks

**Rationale**: User direction explicitly removes Solo Task and Independent Agent from runtime-v2. Runtime-v2 should not translate forbidden modes into Team Tasks because silent translation hides operator intent and can create unexpected legacy state.

**Alternatives considered**:
- Accept legacy-compatible Solo fields and map them to a single-role Team Task: rejected because it preserves obsolete concepts in the runtime-v2 surface.
- Keep Independent Agent seeding in bridge config: rejected because runtime-v2 verification must not create or revive independent-agent work.

## Decision: Role set and start role are explicit request fields

**Rationale**: Runtime-v2 must support arbitrary team starts. The selected role set determines allowed roles; `start_role` must be a member of that set. The legacy workflow may order selected roles canonically, but it must not hardcode PLAN.

**Alternatives considered**:
- Always include PLAN even when the operator asked for DEV+REVIEW: rejected because it hides extra roles and prevents true minimal teams.
- Start from PLAN whenever PLAN exists: rejected for runtime-v2 because the operator explicitly chooses `start_role`.

## Decision: Closing role is PLAN if present, otherwise REVIEW

**Rationale**: Existing CDPA semantics gave PLAN final DONE authority. For teams without PLAN, REVIEW is the natural closing role because it independently verifies acceptance. Teams without PLAN and REVIEW cannot close safely and must be rejected.

**Alternatives considered**:
- Allow any selected role to route DONE: rejected because it weakens review/terminal authority.
- Keep PLAN mandatory: rejected because verified DEV+REVIEW teams are a target runtime-v2 mode.

## Decision: Accepted-send local-chatgpt race is observed, not replayed

**Rationale**: ChatGPT can accept a Send while the URL remains `local-chatgpt:` for a short window. If the exact page and accepted user turn remain present, blocking immediately creates false failures. Replaying after acceptance violates exactly-once. The safe behavior is continued observation until canonical identity is available or provenance is lost.

**Alternatives considered**:
- Immediately block on `local-chatgpt:`: rejected because live verification showed a canonical URL appears after acceptance.
- Restart role/new chat after accepted Send: rejected because it can replay an accepted prompt.
- Backend/global conversation search: rejected unless exact ownership and user-turn provenance are preserved.

## Decision: Reconciliation is separate from restart/new chat

**Rationale**: If the exact accepted page is gone, the system cannot safely recover by resending. It can only create a reconciliation hop that reviews durable reports and ledger evidence without repeating external effects.

**Alternatives considered**:
- Mark the task DONE automatically from ledger evidence: rejected because a human/operator-visible role must verify terminal state.
- Require manual file editing: rejected because it is unsafe and bypasses workflow controls.

## Decision: Runtime-v2 audit is a projection, not execution authority

**Rationale**: 9226/9227 should help operators understand state, not own route/send decisions. This keeps the one-owner rule intact and avoids another duplicated CDPA runtime.

**Alternatives considered**:
- Let dashboard issue hidden route/recovery actions: rejected because it would duplicate legacy worker ownership.
- Poll continuously regardless of tab visibility: rejected because focused lazy refresh provides operator visibility with lower noise and resource use.
