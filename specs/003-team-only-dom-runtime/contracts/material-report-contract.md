# Contract: Material-backed Report Rendering

`/api/report` must render both file-backed and non-file handoffs/reports.

Example response:

```json
{
  "material_id": "mat-...",
  "task_id": "...",
  "source_kind": "inline",
  "material_type": "handoff",
  "version": "v1",
  "mime_type": "text/plain",
  "content_length": 120,
  "truncated": false,
  "content": "...",
  "content_ref": null,
  "error": null
}
```

Gate A dashboard renders bounded inline content only. `content_ref` is opaque/nullable and is not followed by the dashboard in Gate A.

File, route_json, generated_prompt, provider_result, and transcript kinds may be exercised with deterministic fixtures even before their real producers exist.

Unsafe/unavailable materials return a structured visible error, never a blank panel.
