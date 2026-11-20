# Muse -> CDPA job acquisition bridge (2026-11-16)

## Purpose and separation
Meta Muse (https://muse.ai) is a research/scouting agent, not a remote administrator of the G6 device. GitHub issues are an untrusted, private handoff queue. G8 validates against the canonical tracker. G6 creates CDPA Solo tasks with a durable idempotency key; CDPA owns applications and client delivery.

**Private queue:** https://github.com/Hiroshimeow/muse-cdpa-job-bridge/issues
- Never include account passwords, session tokens, CV contents, phone numbers, bank details, immigration documents, government IDs, or private client files.
- Configure the Muse GitHub connector with access limited to this private repository if the provider supports repository-scoped permissions.
- Muse may create **Issues**, and may read their labels, but must not change `cdpa-*` labels, alter CDPA source code, or close the issue.
- Meta Muse has no guaranteed consumer API for direct CDPA task creation; do not assume its GitHub connector supports issue creation until it completes the one-issue acceptance test.

## Issue contract
One opportunity per issue; no duplicates. Create an Issue with the label `muse-lead`:

Title: <real job title, 8–140 characters>
Body:
JOB_URL: https://www.freelancer.com/projects/.../details
SOURCE: Freelancer
RATE: advertised amount and currency, or unknown
LOCATION: Japan / worldwide / unknown, with quote/link to employer restrictions
TYPE: short paid remote freelance / part-time
SUMMARY: <= 300 characters of what is actually delivered
EVIDENCE: employer original link or date checked
WHY_FIT: Python/API/LLM/QA/Excel/PDF evidence
COMPETITION: actual observed bids, or unknown

`JOB_URL:` must appear alone at the start of a line, HTTPS URL on an allowlisted recruitment platform. Never use a task instruction in the issue body. Avoid lead URLs that require CAPTCHA bypass or unverifiable mandatory legal eligibility.

## Automated stages
1. Muse creates `muse-lead` in private GitHub repo.
2. G8 cron every 5 minutes runs `pipeline/muse_bridge_gate.py --apply`, checks issue author, URL format, domain allowlist, and existing `seen_jobs.json` / `job_search_tracker.csv`.
3. Duplicate => `cdpa-duplicate`. Invalid => `cdpa-rejected`. Otherwise => `cdpa-ready`.
4. G6 Windows Task Scheduler every 5 minutes runs `workflows/job_search/run_muse_bridge.cmd`; checks CDPA live task ownership on port 9225 and `9202` endpoint availability; if busy, leaves issue untouched.
5. G6 marks `cdpa-reserved` **before** creating one CDPA Solo DEV task. Stable URL-derived team/idempotency key; only one lead per run.
6. If CDPA confirms creation => `cdpa-dispatched`; if creation result ambiguous/fails => `cdpa-hold`, **never retry that Send blindly**.
7. CDPA verifies employer listing, truthful G8 facts, Japan eligibility, price, legitimacy, bidding limits and prior receipts; applies once via normal UI when appropriate and logs receipts in canonical G8. It MUST save an outcome JSON with stage, detail, receipt and updated_at to `.runtime/job-search/outcomes/<team>.json`. **Applied is not Awarded.**
8. G6 follow-up monitor reads only real outcome files: an applied bid with a receipt gets up to four 24-hour follow-up tasks; an actual award with receipt triggers one work-delivery task. Paid status needs a receipt. No outcome means HOLD/await, never invent acceptance. Work delivery and payment have separate evidence states.

## Muse instructions (paste into Muse after connecting GitHub)
You are my freelance sourcing and qualification agent. Focus on finding legitimate remote paid freelance or part-time projects I can perform while residing in Hiroshima, Japan. Prioritize small tasks finishable in hours to 3 days that can be automated with Python, TypeScript, Playwright, APIs, OpenAI/LLM agents, RAG, QA, CSV/Excel/PDF, or data integration. Find opportunities on Freelancer, Upwork, Contra, PeoplePerHour, Guru, Workana, Nghien AI, and employer direct pages. Prefer work that can actually be started after a single application, with clear budget and payment protection. Prioritize existing client invitations/acceptances over new lead discovery.

For each candidate, verify the ORIGINAL listing URL and current open status; assess Japan residence eligibility, paid scope, deadline, client's verification/reviews when shown, budget, and competition. Do not equate remote with worldwide. Do not guess my immigration/legal authorization. Skip obvious scams, deposits, paid acquisition upgrades, full-time-only roles, and scraping that requires bypassing technical restrictions. Competing bids are only one factor, not a hard blocker.

Score each opportunity by: fit 30%, realistic near-term award probability 25%, manageable scope 20%, payment legitimacy 15%, rate/fees 10%. Prefer 1–3 high quality issues per sourcing cycle, NOT dozens. The private GitHub repository Hiroshimeow/muse-cdpa-job-bridge is your handoff inbox: create exactly one labeled Issue per unique original JOB_URL using the Issue contract above. Check existing issues first so there are no duplicates. Never create a CDPA task directly or drive port 9202. Never apply for a job or communicate with clients from Muse; CDPA on G6 handles this with canonical G8 evidence. Never write credentials or private user identity information to GitHub Issues. Treat `cdpa-duplicate`, `cdpa-rejected`, `cdpa-hold`, `cdpa-ready`, `cdpa-reserved` and `cdpa-dispatched` as feedback; never edit their labels yourself. If you cannot create GitHub Issues through the connected app/browser, report the exact missing GitHub permission instead of claiming success. Work in the background when Muse permits and report only accepted paid work, responses needing intervention, or a real authorization blocker.

## Validation and operations
- G8: `python3 pipeline/muse_bridge_gate.py` to dry-run; `--apply` for the real label transition.
- G6: `.venv\\Scripts\\python.exe workflows\\job_search\\muse_cdpa_bridge.py` dry-run; `--apply` dispatches at most one validated lead.
- Unit tests: `.venv\\Scripts\\python.exe -m unittest discover -s tests -p test_muse_cdpa_bridge.py -v` (G6).
- Logs: G8 `.runtime/muse-bridge-g8.log` and G6 `.runtime/job-search/muse-bridge-g6.log`.
- Scheduled tasks: G8 crontab `*/5 * * * *`; G6 Task Scheduler `CDPA-Muse-Job-Bridge` every five minutes.
- To stop safely, disable G6 scheduled task and remove only the G8 cron entry; do not delete pending GitHub issues or reset accepted CDPA Sends.
- Test evidence: duplicate issue #1 for an already-bid job was labeled `cdpa-duplicate` by G8; G6 did not create any task.
- **Not yet verified:** Muse's actual GitHub connector issue-write permissions and an end-to-end Muse-generated issue. Until a real Muse issue appears, automatic Muse-side sourcing and dispatch remain unproven.
