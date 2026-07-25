from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME_PROMPT = ROOT / "prompts" / "cdpa" / "MAINTAINERS.md"
PACKAGED_PROMPT = (
    ROOT
    / "src"
    / "playwright_auto"
    / "cdpa_defaults"
    / "prompts"
    / "cdpa"
    / "MAINTAINERS.md"
)


def test_maintainers_prompt_states_recovery_boundary_and_lesson_rule():
    prompt = RUNTIME_PROMPT.read_text(encoding="utf-8")

    assert PACKAGED_PROMPT.read_text(encoding="utf-8") == prompt
    assert "recover, diagnose, prevent recurrence, and delegate repairs" in prompt
    assert "smallest safe action" in prompt
    assert "worker alone validates and applies" in prompt
    assert "without waiting for user approval" in prompt
    assert "verified role-offline true list" in prompt
    assert "call the affected team through ROUTE_PLAN" in prompt
    assert "PLAN selects DEV, TEST, REVIEW, or AUDIT" in prompt
    assert "worker automatically appends" in prompt
    assert "not already present in CURRENT LEARNING.md" in prompt
    assert "Use `null` only when no new reusable lesson exists" in prompt
    assert "repository-root `PROBLEM.md`" in prompt
    assert "one stable root cause rather than one entry per incident" in prompt
    assert "Do not record an intentional operator action itself as a problem" in prompt
    assert "include an exact `PROBLEM.md update` section" in prompt
    assert "never claim that the file changed when it did not" in prompt


def test_maintainers_prompt_states_autonomy_repair_and_operator_invariants():
    prompt = RUNTIME_PROMPT.read_text(encoding="utf-8")

    assert "default recovery authority" in prompt
    assert "accepted-send receipts" in prompt
    assert "at most three" in prompt
    assert "CONTINUE_IN_PARALLEL" in prompt
    assert "HOLD_FOR_REPAIR" in prompt
    assert "operator Pause, Stop, Restart role, New Chat, Clear Team" in prompt
    assert "at most three evidence-recorded attempts" in prompt
    assert "bounded CDP round trip" in prompt
    assert "MCP/tooling is distinct" in prompt
    assert "preflighted by the worker before browser acquisition or Send" in prompt
    assert "auth-profile reference" in prompt
    assert "credentials remain worker-runtime-only" in prompt
    assert "authenticated Streamable HTTP lifecycle" in prompt
    assert "performs `initialize`" in prompt
    assert "only when the server returns a session ID" in prompt
    assert "accepts only an empty successful notification response" in prompt
    assert "verifies `tools/list` contains every required capability" in prompt
    assert "requires successful deletion of that temporary session" in prompt
    assert "Stateless servers use `initialize` followed directly by `tools/list`" in prompt
    assert "bounded no-redirect probe of the exact relevant endpoint" in prompt
    assert "Repair creation is valid only through the version-2 top-level `repair` object" in prompt
    assert "Never emit `CREATE_REPAIR_TASK` as a legacy action or as a recovery step" in prompt
    assert "root cause and reason are at most 1200 characters each" in prompt
    assert "reproduction is at most 2400 characters" in prompt
    assert "source areas contain 1–8 allowlisted values" in prompt
    assert "required tests contain 1–16 one-line items" in prompt
    assert "lesson is null or one paragraph of at most 600 characters" in prompt
    assert "Every declared textual field must already be a JSON string" in prompt
    assert "checks the raw array length before trimming" in prompt
    assert "requires every item to be a string" in prompt
    assert "rejects exact duplicates" in prompt
    assert "become duplicates after trimming" in prompt
    assert "never repairs malformed input with `str()` coercion or silent duplicate collapse" in prompt
    assert "sanitized allowlisted projection" in prompt
    assert "URL path credentials are secret material too" in prompt
    assert "exact or tokenized compound high-risk route markers" in prompt
    assert "camelCase/acronym boundaries are canonicalized" in prompt
    assert "bounded exact credential-operation grammar" in prompt
    assert "explicit qualifier+noun pairs" in prompt
    assert "explicit operation+suffix rules" in prompt
    assert "exact compact identities only" in prompt
    assert "generic prefix, suffix, and substring matching are forbidden" in prompt
    assert "marker segment itself and every remaining non-empty path segment" in prompt
    assert "including bare markers and marker-plus-payload forms" in prompt
    assert "static intermediary labels, version segments, callbacks" in prompt
    assert "unprobeable without an explicit worker-owned secret-free descriptor" in prompt
    assert "exception-derived block, waiting, role, hop, refresh, cleanup" in prompt
    assert "dashboard operational projections sanitize them again" in prompt
    assert "excludes stored full Maintainers prompts plus raw evidence arrays" in prompt
    assert "keep the incident suspended rather than persisting or probing the secret-bearing URL" in prompt
    assert "unallowlisted descriptors" in prompt
    assert "unrelated HTTP success are never recovery evidence" in prompt
    assert "repair DONE" in prompt
    assert '"version":2' in prompt
