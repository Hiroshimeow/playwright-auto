# Muse integration — Freelancer bid memory

This is the operating contract for a **future authorized Muse integration**, not a deployed gate. The CLI reads/writes local memory only. It cannot authorize, submit, retry, or check a Freelancer bid. Keep the playbook, the 48-hour web-research cycle, and existing per-team outcomes/receipts. Those systems still own policy, research and platform/dispatch evidence; this palace is the learning index. Do not enable bidding, restart loops, change the profile, or change the coordinator to adopt this document alone.

## Every candidate, before a proposal exists

1. Check the candidate is for `@duonghcu`, eligible for an operator in Japan, and suitable for written work and written communication. Choose **one** primary class slug from `stats`; use `unclassified` only if classification genuinely cannot be recovered.
2. **Before drafting any proposal**, run `recall --class CLASS` and add a keyword if a specific issue matters. Keep the class, relevant tactic IDs and any exception reason in the existing candidate notes. If recall fails, hold the draft and repair the memory issue; never treat a CLI error as “no history.” A class with zero verified bids is valid, not a block. If changing the class or materially changing the brief, recall again before redrafting.
3. Read **About the Client** on the public project page. Capture only observed flags; a null API `owner_id` is not a flag. If the block is unavailable, leave flags unknown and hold if eligibility or safety cannot be established. Aim for age under 2 hours and no more than 25 competing bids unless exceptional. Price near a current comparable average only if viable for scope, effort, and the current authorized floor. Do not substitute a later average or historical quote for a missing bid-time number.
4. Draft 120–200 words: a brief-specific opening line, truthful fit and approach, a small sample-first offer, and exactly one closing question. Keep it natural in the client's written language; do not claim unverified experience. Check the relevant failure notes before an authorized attempt. Profile changes need separate authority and truthful skills. No amount of positive memory overrides the operator's current scope, funding or permission rules.
5. Before the existing executor's irreversible boundary, keep a stable **attempt ID** and the actual bid-time snapshot in the existing operation notes/receipt owner: project ID, class, quote/currency/basis, delivery days or weekly limit, average in that currency/basis, listing age in minutes, competing count, visible client flags, and IDs of tactics **actually applied**. Give separate attempts distinct IDs; don't mint a fresh ID for the same uncertain attempt. Record the actual send time with offset only if known. Do not pre-label an attempt as submitted.

## After every attempt and every outcome change

6. In the same ops turn, append the first `record` for the attempt, whether submitted, failed, blocked, ambiguous, or screened out after entering bid evaluation (`not_submitted`). Use a stable source reference and concise lesson. `submitted` needs a verified Freelancer **bid** ID. Use `--root-cause` for `failed`, `blocked`, or `ambiguous`: if unknown, say what was observed and what is missing. `recall --failures` shows incident history even after reconciliation; the `failures/` cards summarize reusable patterns rather than copying incident counts. For ambiguity, preserve evidence and stop for read-only reconciliation; never resend because a project or result vanished.
7. The existing watch/outcome flow remains authoritative. On **each newly verified change** (substantive reply, award awaiting funding, funded award, closed without award, freeze, withdrawal, receipt reconciliation, or correction), run `record --attempt-id SAME_ID --event-id STABLE_EVENT_ID`, passing only changed outcome fields plus source. Derive `event-id` from the existing observation identity (e.g., `<attempt-id>-<watch-sequence>`); it must be unique across the palace. Replay the *same* command/ID after an uncertain local write; `already recorded` means success. If reconciling an ambiguous attempt to `submitted`, pass the verified `--bid-id` on that same attempt, not a second initial record. Confirm before writing an out-of-order observation as current evidence. Omit fields that are unchanged. `--replied yes` means a substantive client reply about scope, sample, price or timing; ignore automated/spam. Leave unknown when not checked; use `no` only with adequate negative evidence, not silence on a fresh bid. `awarded` means accepted and first milestone funded; a platform award without verified funding is `award_pending_funding`. Award does not imply reply. If state changes upstream without changing our metrics (e.g., paid), record a new `--lesson` naming that change and its `--source`, leaving `--result` and `--replied` unchanged. Keep the existing payment and delivery tracking intact.
8. Do not close the ops turn with an unrecorded bid or outcome change. If a memory write fails, keep the pending owner/receipt evidence, report the exact error, and replay **only the local command**, never the bid. For `.write-lock`, wait for a live writer; if confirmed all palace writers are stopped and the directory is stale and empty, have the operator remove only that lock, check `stats`, and replay the same command. Never edit or delete a published ledger file to clear an error.

## Research and weekly review

9. Keep the **48-hour web-research cycle** unchanged. Bring useful dated findings into `lesson` with a stable evidence ID, citation and `--kind guidance`; research is not proof that a tactic was used on an account bid. Do not rewrite historical snapshots from research. Use `support` and `counter` only for cited firsthand observations; procedural incident evidence is not reply or award lift.
10. **Every Monday 09:00 JST or the first ops run afterward**, after reconciling outstanding observations, run `stats` and `stats --json`. In the existing Muse weekly notes, save the date, class/tactic numerators, denominators, unresolved counts, and the exact cohorts/periods being compared. Inspect failures, unknowns, untagged bids, and pricing/competition patterns. Treat fresh bids and unmatched unknown coverage as non-comparable (use at least 72h for reply comparisons, as the local strategy proposes; not a hard outcome timeout). Stats is cumulative; use its snapshots and inspect `bid_at`/ledger references to compare like classes, age, price basis and sample size. Do not pool INR/USD or fixed/hourly prices, sum overlapping tactics, or infer causation from association.
11. If an evidence-backed shift is seen at reconciliation or weekly review, add its cited evidence through `lesson` with counts and cohort context. Keep the default rule for thin or ambiguous samples; if signals support a change, update `--rule` or create a **new tactic ID** for a materially different experiment and compare one change at a time. Add a redacted opening to `language/openings-evidence.json` only if a substantive reply can be tied to that bid; note language, bid ID, and receipt/event reference, not private conversation text. Log the decision and next check in the weekly notes even if nothing changes.

## Exact CLI calls (PowerShell)

Use an absolute script path so the default palace never depends on working directory. `-X utf8` preserves Vietnamese and punctuation. Replace the demonstration IDs, measurements and evidence with real observations; the `record` and `lesson` examples **write local files** and should not be run with their sample data in production.

```powershell
$py = 'E:\git-project\cdpa\.venv\Scripts\python.exe'
$bm = 'E:\git-project\cdpa\workflows\job_search\bid_memory\bid_memory.py'
& $py -B -X utf8 $bm recall --class pdf-to-word 'milestone'
if ($LASTEXITCODE -ne 0) { throw 'Recall failed; hold proposal draft' }

# Immediately after a verified receipt. Repeat --tactic and --client-flag as needed.
& $py -B -X utf8 $bm record --attempt-id 'p12345-attempt-1' --project-id 12345 --bid-id 98765 --class pdf-to-word --amount 16000 --currency INR --basis fixed --duration-days 3 --market-average 16750 --age-minutes 51 --bid-count 12 --bid-at '2026-11-19T14:05:00+09:00' --client-flag payment-verified --tactic sample-first --result submitted --lesson 'Proposed a one-page layout sample' --source 'receipt:98765'
if ($LASTEXITCODE -ne 0) { throw 'Preserve receipt; retry only the local record' }

# Same attempt, distinct observations. Unchanged state does not need a fresh update.
& $py -B -X utf8 $bm record --attempt-id 'p12345-attempt-1' --event-id 'p12345-attempt-1-watch-1' --replied yes --lesson 'Client clarified which tables must stay editable' --source 'watch:98765:1'
if ($LASTEXITCODE -ne 0) { throw 'Preserve observation; retry only the local record' }
& $py -B -X utf8 $bm record --attempt-id 'p12345-attempt-1' --event-id 'p12345-attempt-1-watch-2' --result awarded --source 'funding-receipt:98765'
if ($LASTEXITCODE -ne 0) { throw 'Preserve observation; retry only the local record' }

# Different attempt, unresolved receipt. Capture any bid-time facts that are known.
& $py -B -X utf8 $bm record --attempt-id 'p23456-attempt-1' --project-id 23456 --class data-entry --result ambiguous --root-cause 'Executor returned AMBIGUOUS; send boundary not verified' --lesson 'Parked for exact receipt reconciliation; no replay' --source 'executor:p23456-attempt-1'
if ($LASTEXITCODE -ne 0) { throw 'Preserve incident; retry only the local record' }
& $py -B -X utf8 $bm recall --failures '23456'

& $py -B -X utf8 $bm stats
& $py -B -X utf8 $bm stats --json
# Illustrative evidence only. Adding evidence without --rule leaves the rule unchanged.
& $py -B -X utf8 $bm lesson sample-first --evidence-id 'week-2026-11-23-pdf' --kind support --evidence '3 of 12 mature PDF bids with this tag received replies; sample size thin, keep rule' --source 'Muse weekly review 2026-11-23, PDF cohort'
```
