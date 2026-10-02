# CDPA Solo Task Contract

## Canonical request

The supported Solo create payload is:

```json
{
  "workflow_mode": "solo",
  "role": "REVIEW",
  "task": "Short task instruction",
  "goal": "Full one-role goal",
  "requested_team": "unique-solo-team-name",
  "browser_endpoint_id": "9222",
  "repository": "E:\\git-project\\target"
}
```

Optional:

```json
{
  "conversation_url": "https://chatgpt.com/c/<conversation-id>"
}
```

The API resolves `browser_endpoint_id` and internally pins Solo work to that exact endpoint. Do not send `required_browser_endpoint_id`; it is a Team/manual-run field and the Solo API derives it.

Solo rejects Team Task fields including `roles`, `reuse_team`, `new_roles`, `new_all`, `bootstrap_id`, `bootstrap_definition`, `temporary`, and `required_browser_endpoint_id`.

`conversation_url`, when present, must be an exact `https://chatgpt.com/c/<id>` or `https://www.chatgpt.com/c/<id>` URL without query or fragment.

## Canonical CLI

For a single Solo task, the public CLI is:

```powershell
cdpa "<task>" `
  --solo REVIEW `
  --goal "<goal>" `
  --browser 9222 `
  --team <unique-team> `
  --repository "E:\git-project\target"
```

Optional persistent conversation reuse:

```powershell
  --conversation-url "https://chatgpt.com/c/<id>"
```

Solo CLI accepts only the Solo-compatible task flags. Do not add `--new`, `--new-all`, `--temp`, `--depends-on`, uploads, or bootstrap fields.

For self-spawning chains, prefer `scripts/spawn_solo_task.py` because it uses deterministic idempotency and avoids duplicate children on retries.

## Idempotency

Every mutation to `POST /api/tasks` requires `Idempotency-Key`.

The helper derives a stable key from the exact Solo payload when one is not supplied. Repeating the same child creation therefore reuses the same command/task instead of creating a duplicate.

Do not change the key only because the child remains queued for a while. The worker applies create commands asynchronously.

## Long-lived conversation reuse

A Solo Task may start Fresh or adopt an exact old `conversation_url`. Reusing a URL does not mean reusing the previous operation's result, baseline, or task identity.

When a chain deliberately continues on an existing conversation:
- pass the exact durable `/c/...` URL explicitly;
- keep the new Solo task/task ID distinct;
- let current repository/runtime evidence determine what remains;
- do not assume previous chat conclusions are still valid.

## Chain role selection

Default engineering chain after a plan already exists:

1. `REVIEW` challenges/simplifies the plan and spawns DEV.
2. `DEV` implements, verifies, commits when authorized, then spawns REVIEW.
3. `REVIEW` independently checks the diff/evidence.
   - concrete defect -> spawn DEV with exact finding;
   - code clean but runtime proof missing -> spawn TEST;
   - all code/runtime acceptance already proven -> act as final closure REVIEW and stop.
4. `TEST` performs independent runtime/acceptance proof and then spawns REVIEW closure or DEV if it finds a defect.

Do not create PLAN/TEST/AUDIT merely for ceremony. Use the smallest next role that can discharge the remaining evidence or work.

## Endpoint selection

A Solo create request always pins exactly one concrete browser endpoint. If the operator explicitly specifies one, use it. Otherwise consider both endpoint `9212` and endpoint `9222`, inspect current active-tab usage against `max_active_tabs`, and select an endpoint with a free slot. Do not blindly default to `9202`.

The endpoint choice is made before creation; do not send multiple endpoint IDs in one Solo request.

## Parent completion

If work or independent verification remains, the current Solo task MUST NOT stop. It must successfully submit exactly one next Solo child.

POST acceptance alone is not enough for parent completion. After receiving the accepted child `task_id`, wait for that exact child to materialize and become visible through `GET /api/tasks/<task-id>` / the dashboard. A transient 404 means wait and retry the same task ID, never create another child.

Only after the child is visible may a non-final parent record that child task ID/team in its report/handoff and return its Solo `DONE`.

The route worker is not the chain mechanism. Solo final output may remain the standard `DONE`/`PAUSE` JSON contract; the child already exists independently and is visible to the control plane.

## Failure handling

- POST returned task ID/queued command -> child submission was accepted. Do not duplicate it.
- Same idempotency key + same payload -> safe retry.
- Same idempotency key + different payload -> conflict; correct the caller, do not mint random retries blindly.
- 400 invalid request -> inspect payload against this contract; most often a Team-only field or missing browser endpoint.
- Child not yet visible via GET -> the non-final parent must keep waiting/checking the same task ID and must not finish or create another child.
- Genuine external/manual prerequisite -> PAUSE may be correct; document resume condition instead of spawning a useless loop.
