# Quickstart Validation: Solo Existing-Conversation Correctness

## Prerequisites

- Run from repository root.
- Use the existing Python environment and repository-native pytest setup.
- Preserve unrelated dirty/untracked work.
- Do not reset, stash, clean, switch branches, push, or create a second implementation path.

## Focused deterministic checks

Run the narrow tests for the three reproduced defects first:

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests/test_cdpa_solo_task.py `
  tests/test_cdpa_runtime_stability.py `
  tests/test_cdpa_actions.py `
  tests/test_cdpa_commands.py
```

Expected outcomes:

- project-scoped ChatGPT conversation URLs are accepted;
- the exact user navigation URL is preserved;
- project-scoped and short-form URLs compare as one conversation identity;
- a new Solo task reusing an existing conversation sends its current Goal in the first substantive prompt;
- an already-responding conversation enters receiptless wait with zero send attempts;
- a ready-to-sending response race also enters receiptless wait only while the durable request remains pristine;
- the old assistant response is not consumed as the new task result;
- when old activity ends, the pending new Goal is sent exactly once;
- post-boundary/ambiguous sends still use exactly-once reconciliation;
- terminal historical ownership does not false-block; non-terminal foreign ownership still does.

## Regression checks

After focused GREEN:

```powershell
.venv\Scripts\python.exe -m pytest -q `
  tests/test_operator_resume_recovery.py `
  tests/test_cdpa_worker.py `
  tests/test_chatgpt_safety.py
```

Run broader repository tests if the changed shared URL/ownership boundary causes cross-module effects:

```powershell
.venv\Scripts\python.exe -m pytest -q
```

Finally:

```powershell
git diff --check
```

## Runtime acceptance scenario

Use a known existing ChatGPT conversation under the authorized browser endpoint.

### Scenario A: Project-scoped idle URL

Create a Solo Task with an existing project-scoped URL ending in `/c/<id>` and a distinctive Goal token such as `SOLO_GOAL_ACCEPTANCE_20260930`.

Verify:

1. task creation succeeds;
2. the browser opens the exact supplied project-scoped URL;
3. the first substantive prompt contains the distinctive Goal;
4. there is exactly one Send attempt for the new Goal.

### Scenario B: Project-scoped URL already responding

While the same conversation is actively generating a response, create the new Solo Task.

Verify:

1. the task stays RUNNING/WAITING rather than BLOCKED;
2. no new task prompt is sent while the prior response remains active;
3. the prior assistant response is not routed as the result of the new task;
4. after prior response activity ends, the new task sends its full Goal exactly once;
5. the request ledger proves no duplicate send.

### Scenario C: Sending race

Arrange deterministic timing where preparation reports ready, then the conversation begins responding before the atomic send.

Verify:

1. hop may have entered `sending`, but the ledger remains pristine/zero-attempt;
2. `page is already responding` transitions to receiptless observation instead of `unexpected_error`;
3. after prior response ends, the exact prepared request resumes without rebuild/rebind drift and sends once.

## Evidence to retain

- task id/team;
- original supplied URL;
- resulting `conversation_url`/page URL;
- hop state transitions;
- request-ledger status/attempts/receipt;
- prompt text proving Goal delivery;
- focused test output.
