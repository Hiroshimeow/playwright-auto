# CDPA Current Verified State

Date: 2026-10-25

## Scope

This repository is the clean G6 rebuild baseline. The completed scope is Runtime v2 / R11 through Spec003 Gate B, using FJP `runtime_v3` as the active implementation donor. Gate C (T074+) remains deliberately open.

The CDPA runtime is intentionally OFF. Do not enable the G6 dashboard/API/worker until the separate browser/CDP release acceptance is completed.

## Authority and provenance

- Architecture and acceptance authority: `docs/reference/r11/`.
- Rebuild contract: `docs/REBUILD_CONTRACT.md`.
- Canonical R11 ancestor: `0a80878fec4c7c92ed70fdb7e59ff98498be615e`.
- G6 repository: `E:\git-project\cdpa`.
- G6 branch: `rebuild/runtime-v2-r11`.
- Verified runtime/test HEAD before this evidence-only documentation update: `6bac3efd1c35c4bbcf6e2f229cf3e720593103aa`.
- Active donor: FJP `C:\Users\DuongNH66\Desktop\git\cdpa`, branch `runtime_v3`.
- The old G6 CDPA repositories, worktrees, audit snapshots, dashboard/API runtime and PM2 CDPA entries were deliberately retired.

If a Spec003 document conflicts with R11, R11 wins.

## Rebuild checkpoints

- `0cd7432` restore the exact Runtime-v2 `0a80878` base tree.
- `1e887fe` lock R11 design/acceptance authority.
- `b656ff0` restore durable TeamTask/idempotency and lost-ack safety.
- `1faa8ef` converge authoritative DOM + RoleController behavior.
- `5e66571` integrate Gate B provider/async/dependency/material/runtime owners.
- `276edbe` disable legacy Independent activation by default.
- `0a7dad0`, `ca0b782`, `67e2045`, `51535c8` align and harden regression contracts/resources.

## Donor parity

The FJP working-tree production delta was captured owner-by-owner into a temporary transfer branch and audited by file/blob hash.

- FJP working tree -> transfer branch: zero production mismatches under `src/playwright_auto`.
- G6 rebuild HEAD -> transfer branch: zero differences under `src/playwright_auto`.
- Root runtime files `cdpa.yaml`, `package.json`, `pyproject.toml`, and `uv.lock`: no donor delta.
- G6 intentionally retains stricter/hardened tests where they improve determinism or enforce the R11 default-off migration policy.

The transfer branch is transport evidence only; it is not architecture authority and should be deleted after final verification.

## Implemented state through Gate B

- TeamTask is the public runtime-v2 execution model.
- Stable idempotency identity survives lost acknowledgements and preserves implicit team identity.
- Fresh DOM projection is authoritative for browser state; LISTEN/network state cannot override it.
- RoleController owns serialized browser mutation.
- No implicit self-route; same-role unfinished work stays in the same turn.
- Temporary Chat does not auto-F5/reload as recovery.
- ProviderDispatcher owns provider selection.
- Browser DOM delegates to the browser/RoleController path; non-browser work uses ProviderJob/ProviderResult.
- Material APIs, AsyncContinuation, CronJob, Heartbeat and TeamTask dependency readiness are integrated under existing owners.
- Legacy Independent Agent activation is migration-only and defaults OFF.
- Gate B tasks T059-T073 are complete; Gate C T074+ remains open.

## Focused verification on G6

- Spec003 Gate B: 65 passed.
- Spec003 Phase 1.5: 26 passed.
- Dependency/lifecycle owner set: 29 passed.
- R11 critical runtime/attach set: 63 passed.
- Role/controller convergence: 70 passed.
- ChatGPT DOM/model/permission: 60 passed.
- Dashboard API: 26 passed.
- Core config/runtime: 33 passed.
- Prompt contracts: 28 passed, 4 skipped.
- Independent migration coverage: 23 command tests + 15 worker tests + 10 UI tests passed.
- Browser role runtime: 16 passed.
- V11 DOM allow-click policy after deterministic resource cleanup: 7 passed.
- `python -m compileall -q src/playwright_auto`: pass.
- `git diff --check`: pass.

## Broad regression

First complete G6 full-suite run:

- 1927 passed, 45 skipped.
- Test assertions all passed.
- A post-suite Windows tempfile cleanup warning exposed one cross-module RuntimeDB/TemporaryDirectory leak in a test helper.
- The leaking importer was corrected and its focused suite passes with clean stderr.

A second full-suite run is required to prove clean process teardown after that fix. Do not treat this baseline as final until the second run is recorded below.

### Clean-teardown rerun

- 1927 passed, 45 skipped in 446.56s (0:07:26).
- stderr: 0 bytes.
- No post-suite tempfile/SQLite cleanup traceback.
- Result: PASS.

## Runtime/offline acceptance

At the verified checkpoint:

- `E:\git-project` contains only one CDPA-related repository: `cdpa`.
- G6 CDPA API/dashboard ports 9224/9225 have no listeners.
- No CDPA worker/dashboard/API process is running.
- Legacy PM2 CDPA entries were removed; unrelated `pi-web*` services were preserved.
- G6 Git has one worktree only: `E:/git-project/cdpa`.
- Python virtual environment: CPython 3.12.12.
- `uv.lock` is present.

## Next build boundary

Continue at Gate C T074+. Do not reopen Gate B architecture with parallel stores/controllers/schedulers.

Before re-enabling the G6 runtime, separately prove the browser/CDP release gates in `docs/REBUILD_CONTRACT.md`, including exact ownership, Temporary behavior, no focus stealing, no duplicate irreversible action, and real browser acceptance.
