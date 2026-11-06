# CDPA Self-Evolution — incorrect Solo agent prompt root cause

Date: 2026-11-07. Scope: G6 E:\git-project\cdpa. Source owner: workflows/self-evolution/spawn_cdpa.py.

## Verified root cause

The previous launcher passed the entire campaign brief under `--task` and omitted `--goal`. CDPA Solo CLI posts these as separate fields: task is a display/title field; goal becomes `state.goal`. The worker calls `task_goal_for_hop(state, hop_id)` and `PromptBuilder.build_solo(goal=...)`. For Solo, this selects `state.goal`, not `state.task_text`. The default blank goal caused the agent to see only a generic 363-character bootstrap and route contract despite a 9KB stored task_text. Verified on live corefix task cdpa-idem-3205a33826a7fe89ccac9a92: task_text 9794 chars and role input 363 chars; all actual bounded instructions absent from the sent turn. The same constructor affected the review and Pi recovery task.

## Fix

`spawn_cdpa.py` now puts a concise display title under `--task` and puts the full, latest GOAL_AND_RULES.md, mandatory spawn command, parent ID and bounded instruction under `--goal`. It invokes Python CLI directly, avoiding CMD multiline truncation. Existing Windows command length guard remains. A regression test now follows generated argv through `PromptBuilder.build_solo` and verifies the full goal and bounded instruction appear in the first-turn prompt.

## Verification

`.venv\Scripts\python.exe -m pytest -q tests/test_self_evolution_propagation.py tests/test_self_evolution_core.py tests/test_cdpa_solo_task.py`: 40 passed, exit 0. No browser Send performed by this patch.

## Existing task reconciliation and continuation

Old task g6-evolution-review-20261107-r2 is BLOCKED, block_code=response_terminal_missing, with a previously accepted Send. g6-evolution-corefix-20261107-r1 became PAUSED; g6-evolution-pi-recovery-20261107-r1 was RUNNING/wait_response on last inspection. They received generic Solo role inputs because the old launcher omitted goal. Do not retry or re-Send their accepted/ambiguous requests. Pi must reconcile exact owned task/Send before creating a replacement task with new unique identity using the corrected constructor. It must prove the actual persisted `active_input.input` contains `Goal: # CDPA Self-Evolution`, bounded instruction and spawn command, and that only one accepted Send occurred on the new task. No claim of full harness acceptance until live evidence and independent review.

Release verdict for full campaign: NOT_ACCEPTED. This bounded incorrect-prompt rootfix: VERIFIED.
