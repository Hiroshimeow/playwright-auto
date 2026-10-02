---
name: using-pi-coding
description: Use when delegating coding, debugging, repository research, review, RCA, patch design, or implementation to pi-coding, especially on Windows/PowerShell or when choosing between gpt Luna, Sol, and Astra or claude with claude-sonnet-4-6, claude-opus-4-6-thinking
---

# Using Pi Coding

## Core rule

Use the **lightest model that can reliably do the job**, grant only the tools it needs, and send long prompts through a file instead of PowerShell quoting.

## Model tier

| Tier | Use for |
|---|---|
| **Luna** | Light/fast: search, summaries, simple edits, focused test/debug tasks |
| **Sol** | High/default: substantial coding, review, RCA, multi-file reasoning |
| **claude-sonnet-4-6** | High: substantial coding, implement|
| **Astra** | Max: hardest architecture/debugging, ambiguous incidents, final high-stakes synthesis |
| **claude-opus-4-6-thinking** | Max: hardest architecture/debugging, ambiguous incidents, final high-stakes synthesis , use when user request|

Resolve the exact available provider/model before the run; do not silently substitute another tier/provider. Useful discovery: `pi --list-models luna`, `pi --list-models sol`, `pi --list-models astra`. **Thinking is a separate capability:** only pass `--thinking <level>` when discovery says that selected model/provider supports thinking; otherwise omit it or use `off`.

## Tool scope

- Pure synthesis from supplied evidence: `--no-tools`
- Read-only repo inspection: `--tools read,grep,find,ls`
- Implementation: add only what is needed, typically `bash,edit,write`
- Never call a run read-only while granting mutation tools. Pi `--tools` values are comma-separated, not space-separated.

## Run patterns

Short prompt:

```powershell
pi --provider <provider> --model <model> --no-session `
  --tools read,grep,find,ls -p "<prompt>"
```

Long/multiline prompt: write UTF-8 to a file, then pipe it. This avoids quote/backtick/newline problems.

```powershell
Get-Content -LiteralPath $prompt -Raw -Encoding UTF8 |
  pi --provider <provider> --model <model> `
     --no-session --tools read,grep,find,ls -p > $out 2> $err
```

When the caller has a short command timeout, run the same pipeline in a background `Start-Process`/`cmd.exe` job, keep its PID, and poll:

```powershell
Get-Process -Id $pid -ErrorAction SilentlyContinue
Get-Item $out,$err -ErrorAction SilentlyContinue | Select Name,Length,LastWriteTime
Get-Content $err -Tail 40 -ErrorAction SilentlyContinue
```

`pi -p` may buffer stdout at **0 bytes until the final answer**. A live process plus a zero-byte output file is not evidence of a hang. Do not spawn a duplicate until the first process is confirmed exited or failed.

## Completion check

A run is complete only when the process exited successfully and the expected output is non-empty. Then read the result and independently verify important claims against repository/runtime truth before acting on it.

## Common mistakes

- Using Astra for trivial work or Luna for a hard architectural incident.
- Hard-coding one provider without checking availability.
- Inlining a long prompt into PowerShell.
- Treating buffered output as a stall.
- Granting `bash/edit/write` for an audit that should be read-only.
- Trusting the subagent memo without verifying its source claims.



