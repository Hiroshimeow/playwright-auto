# ChatGPT workflow system

The workflow layer is built for deterministic browser automation. A user workflow is a plain ordered list of blocks; selectors, recovery and ownership checks stay inside the block/client implementation.

## One-file durable workflow

Copy or edit `workflows/chatgpt_durable_loop.py`:

```python
from pathlib import Path
from playwright_auto.workflow_api import *

VARIABLES = {
    "prompt": "Implement phase 1",
    "files": [],
}

WORKFLOW = Workflow(
    "dev-loop",
    [
        SetRoleBlock("DEV"),
        RecoverPageBlock(),
        WaitCleanReadyBlock(),
        DurableSendBlock(
            variable("prompt"),
            files=variable("files"),
            ledger_path=Path(".runtime/dev-request-ledger.json"),
        ),
    ],
)

LOOP = LoopOptions(
    max_iterations=1,
    stop_file=Path(".runtime/STOP_DEV_LOOP"),
    checkpoint_path=Path(".runtime/dev-loop-checkpoint.json"),
)
```

Run:

```bash
uv run python scripts/run_chatgpt_loop.py workflows/chatgpt_durable_loop.py
```

Reorder, insert or remove blocks only in `WORKFLOW`. `SetRoleBlock` must precede mutating single-tab blocks.

## Standard Send versus durable Send

Use standard blocks when process-restart idempotency is not required:

```python
SendPromptBlock(variable("prompt"), receipt_key="request")
WaitResponseBlock(
    receipt_key="request",
    response_key="answer",
    stable_ms=1000,
    active_reload_after_ms=120_000,
)
```

Use `DurableSendBlock` for loops, process restarts or uploads. It combines:

```text
canonical request identity
ROLE_REQUEST_ID marker
file SHA/size identity
upload readiness
pre-click ledger persistence
SendReceipt persistence
response provenance
cached completion
per-request process lock
```

A durable request never automatically re-clicks after entering `SENDING` unless the transcript proves that the exact marker was accepted.

The idempotency key includes role, normalized prompt, `source_context`, role-prompt hash and file identities. Repeating the same inputs returns the cached request. When each loop iteration represents a distinct external job, include that stable job ID:

```python
DurableSendBlock(
    variable("prompt"),
    source_context=lambda ctx: {"job_id": ctx.require("job_id")},
)
```

Do not use a volatile timestamp as the context; that disables crash idempotency.

## Send and response evidence

Send acceptance is limited to:

1. the exact new user prompt appears after the pre-send baseline; or
2. the Stop button appears.

A new assistant node alone is not Send evidence.

Response completion requires:

1. the exact user prompt after baseline;
2. a new assistant turn after baseline;
3. Stop absent;
4. no manual composer text or attachment conflict;
5. no unresolved choice prompt;
6. structurally complete text, unless the response is image-only;
7. an unchanged message/text/image fingerprint for the stability window.

After an F5/reload recovery, the first complete-looking snapshot is skeptical: it must change again or be observed unchanged on an additional sample.

## Manual input and destructive operations

Defaults are fail closed:

- `SetComposerBlock` does not overwrite non-owned text;
- `ClearComposerBlock` clears only exact owned text;
- `NewChatBlock` refuses to discard drafts or attachments;
- response recovery never reloads while manual steering is present;
- upload refuses attachments without the durable marker or partial existing attachments.

Destructive behavior requires explicit flags:

```python
SetComposerBlock("replacement", overwrite=True)
ClearComposerBlock(force=True)
NewChatBlock(discard_draft=True, discard_attachments=True)
```

## Choice prompts

A choice prompt is not treated as a completed response. By default the workflow fails closed. To resolve it explicitly:

```python
ResolveChoicePromptBlock()
```

Only visible, enabled, positive choices such as Continue/Proceed/Start/Approve are considered. Cancel/Delete/Close/Share/Copy-like controls are excluded.

## Page recovery

```python
RecoverPageBlock(
    allow_new_chat=False,
    resolve_choice_prompt=True,
)
```

Recovery order:

```text
ownership + snapshot health
→ one reload
→ optional safe choice resolution
→ optional New chat only when no manual draft/attachment/dialog exists
→ explicit fresh-tab/rerole failure action
```

Authentication remains manual. A redirect to `auth.openai.com` raises `AuthenticationRequiredError`; the tab title and in-page badge retain `ROLE · TASK-ID · page-id` through `window.name`, so the blocked role stays visible on the 9223 viewer. The engine never edits or submits the authentication page.

## Upload workflow

Standalone upload blocks are available:

```python
SetComposerBlock(variable("marked_prompt"))
UploadFilesBlock(
    variable("files"),
    request_marker=variable("request_marker"),
)
WaitUploadReadyBlock(
    variable("request_marker"),
    expected_count=lambda ctx: len(ctx.require("files")),
)
SendPromptBlock(
    variable("marked_prompt"),
    expected_attachment_count=2,  # exact count already verified above
)
```

For practical workflows prefer `DurableSendBlock`, which computes file identity and owns the upload/send state machine.

Upload readiness requires the exact marker, expected attachment count, editable composer, no active response, and visible enabled Send. Transport tries a file input first and a bounded drag/drop payload fallback second.

## Multi-role workspace

```python
WORKSPACE_ROLES = ("PLAN", "DEV", "REVIEW")

WORKFLOW = Workflow(
    "role-route",
    [
        ParseRouteBlock(variable("route_json")),
        DispatchRouteBlock(parallel=True),
        WaitRouteResponsesBlock(parallel=True),
    ],
)
```

The runner attaches existing requested role tabs and opens missing ones. Dispatch and response wait run independently per physical role tab, collect all outcomes, and only then raise `RoleDispatchError`. Set `fail_on_error=False` to return partial receipts/responses plus an error map.

Route parsing accepts only one exact JSON object or one JSON code fence without surrounding prose. Unknown/inactive roles, duplicate keys, empty prompts and multiple objects are rejected. An optional repair runs once; there is no default-role fallback.

## General multi-role team workflow

Use `WORKSPACE_TEAM` when a role needs multiple visible instances:

```python
WORKSPACE_TEAM = {
    "PLAN": 1,
    "DEV": 3,
    "REVIEW": 4,
    "TEST": 2,
}
WORKSPACE_TIMEOUT_MS = 30_000
```

The resulting tabs are `PLAN`, `DEV`, `DEV1`, `DEV2`, `REVIEW` through `REVIEW3`, `TEST`, and `TEST1`. The base role and instance are separate data; `DEV1` is a visible slot name, not a new role class.

A finite team graph is defined with `TeamConversationBlock`:

```python
TeamConversationBlock([
    TeamRoundSpec("plan", ("PLAN",), plan_prompt, parallel=False),
    TeamRoundSpec("implement", ("DEV*",), implement_prompt, parallel=True),
    TeamRoundSpec("review", ("REVIEW*", "TEST*"), review_prompt, parallel=True),
    TeamRoundSpec("closeout", ("PLAN",), closeout_prompt, parallel=False),
])
```

Each round receives the persisted outputs of earlier rounds. A task switch is preflighted across the whole participating team before any tab is changed. The same task reuses its conversation; a different task opens New Chat only when there is no manual draft, attachment, dialog, or active response.

The checkpoint identity includes task ID, goal digest, workflow version, role set, and round graph. Role attach order does not affect identity. Completed slots are skipped on resume, and exact per-role prompts are immutable once checkpointed.

`DurableSendBlock` adds a request marker and fails closed after an ambiguous send boundary. If a sent marker disappears, the state is `sent_marker_missing`; the engine does not click Send again.

Sustained response testing requires an authenticated profile. Anonymous ChatGPT was sufficient for a completed sequential PLAN → DEV → REVIEW → PLAN workflow, but repeated guest requests later redirected fresh role tabs to authentication. Structural 2/5/10-role browser stress and durable simulated parallel stress passed; authenticated parallel response completion remains a separate runtime gate.

## Retry safety

Generic `RetryBlock` is limited to blocks marked `retry_safe=True`. Composite blocks inherit the least-safe child. These are not generic-retry safe:

```text
ActionBlock by default
SendPromptBlock
DurableSendBlock
UploadFilesBlock
NewChatBlock
RefreshBlock
SetRoleBlock
StopResponseBlock
ResolveChoicePromptBlock
RecoverPageBlock
DispatchRouteBlock
ParseRouteBlock
```

Side-effecting blocks implement their own exact recovery contract.

## Loop and checkpoint

`LoopOptions` supports fixed/infinite iterations, stop-file checks, interval sleep, error policy, cancellation and atomic checkpoint writes. Infinite loops require a stop file.

```python
LoopOptions(
    max_iterations=None,
    interval_seconds=2,
    stop_file=Path(".runtime/STOP"),
    checkpoint_path=Path(".runtime/loop.json"),
)
```

Stop:

```bash
touch .runtime/STOP
```

The loop checkpoint is execution evidence, not automatic block-index replay. Side-effect resume is handled by `DurableSendBlock` and its request ledger.

## Editing existing workflows

Every block has a stable ID:

```python
workflow.replace(
    "durable_send",
    DurableSendBlock("replacement", block_id="durable_send"),
)
workflow.insert_before("durable_send", CaptureSnapshotBlock("before_send"))
workflow.remove("capture_before_send")
```

Failed mutations are transactional: duplicate IDs do not partially corrupt the workflow.

## Block inventory

ChatGPT and durability:

```text
SetRoleBlock
RecoverPageBlock
WaitCleanReadyBlock
NewChatBlock
RefreshBlock
SetComposerBlock
ClearComposerBlock
ResolveChoicePromptBlock
SendPromptBlock
WaitResponseBlock
StopResponseBlock
UploadFilesBlock
WaitUploadReadyBlock
DurableSendBlock
CaptureSnapshotBlock
AssertBlock
SaveRecentResponsesBlock
```

Control flow:

```text
ActionBlock
SequenceBlock
WhenBlock
RetryBlock
TimeoutBlock
TryBlock
RepeatBlock
WaitUntilBlock
DelayBlock
SetVariableBlock
```

Workspace:

```text
RoleSequenceBlock
ParseRouteBlock
DispatchRouteBlock
WaitRouteResponsesBlock
```
