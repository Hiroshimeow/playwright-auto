# Contract: Material API

## MaterialRef vocabulary

`source_kind` is one of:

- file
- inline
- api
- generated_prompt
- route_json
- provider_result
- transcript

`material_type` is one of:

- report
- handoff
- prompt
- skill
- rule
- task_context
- log
- artifact

`version` is an opaque string. `content_ref` is opaque and nullable.

## Gate A

Existing `/api/report` is preserved/bridged to return bounded inline display content plus metadata such as material_id, source_kind, material_type, version, content_length, truncated, mime_type, and structured error state.

The Gate A dashboard renders inline content and does not dereference content_ref.

## Gate B

Full material dereferencing endpoints may be added for provider curl/API access, including handoff, prompt, skill, rule, report, task context, logs, and artifacts.

Only registered task/runtime materials may be served. Arbitrary filesystem paths are rejected. Existing runtime bind/auth policy remains authoritative.

## Access boundary

The material API reuses the existing runtime bind/auth policy and is intended for the local runtime/provider boundary. Requests may resolve only registered MaterialRefs associated with the addressed task/runtime context; arbitrary filesystem paths and cross-task guessing are rejected. No new public network exposure is introduced by this feature.
