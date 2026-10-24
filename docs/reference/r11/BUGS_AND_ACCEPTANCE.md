# Architecture regression set

These incidents are acceptance tests for runtime-v2, not a backlog of unrelated patches.

## A1 — response ended but task waits forever

Observed shape:

- task remains `waiting`;
- Stop is not visible;
- response is no longer actually active.

Acceptance:

- controller re-reads current DOM;
- if a valid stable terminal route/report exists, accept it;
- if response is inactive and candidate is incomplete/invalid, apply only the correct bounded path;
- do not wait forever because a stale activity bit/listener said streaming;
- absence of Stop alone is not terminal proof.

## A2 — self-route / no-op route response

Observed:

- role routes to itself to continue unfinished implementation/tests;
- or emits route JSON almost immediately without substantive work.

Acceptance:

- self-route absent from default route enum;
- prompt prose cannot request a destination omitted from schema;
- unfinished work stays in the same turn;
- a route ends a role turn and therefore must represent a real handoff/closure condition;
- explicit bounded self-route is opt-in only.

## A3 — automatic tab focus

Acceptance:

- normal observe/send/wait/route code never calls focus/bring-to-front;
- background page reads work without UI focus;
- tests fail if normal controller path invokes focus.

## A4 — repair despite valid result

Acceptance:

- current valid terminal route+report has priority over Retry/repair/stale diagnostics;
- malformed repair starts only from a stable, inactive, current invalid candidate;
- an empty assistant shell is scaffolding/WAIT, not malformed output;
- repair attempts are not consumed while content is still changing/streaming.

## A5 — trustworthy DOM signals ignored

Signals to normalize once:

- MCP/tool permission + Allow action;
- Retry / `Message delivery timed out`;
- connection interrupted/error banner;
- Stop/active generation;
- composer readiness/manual content/attachments;
- current assistant text and terminal route/report.

Acceptance:

- DOM signal drives deterministic controller decision;
- provider permission is never parsed as route output;
- Retry/error from an older failed generation does not block a safe new pre-send composer;
- Retry/interrupt during active response does not prematurely trigger route repair;
- no LISTEN/network signal can overrule fresh valid DOM terminal evidence.

## Legacy regression cases to retain

- `Start Voice` must not be a generic choice blocker.
- Duplicate DOM representation of the same user message must not block user steering.
- `Allow once` without `Approval options` must still be actionable.
- Temporary BLOCKED must preserve the tab.
- `page is already responding` must WAIT, not BLOCK.
- exact pristine durable pre-boundary request may resume same request identity.
- recovery must preserve unrelated manual composer text/attachments.
- canonical conversation identity must prevent silent New Chat replacement.