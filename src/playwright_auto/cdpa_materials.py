from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, MutableMapping

SOURCE_KINDS = frozenset(
    {"file", "inline", "api", "generated_prompt", "route_json", "provider_result", "transcript"}
)
MATERIAL_TYPES = frozenset(
    {"report", "handoff", "prompt", "skill", "rule", "task_context", "log", "artifact"}
)
DEFAULT_INLINE_LIMIT = 64 * 1024


class MaterialRefError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class MaterialRef:
    material_id: str
    task_id: str
    source_kind: str
    material_type: str
    version: str
    mime_type: str
    content_length: int
    truncated: bool
    content: str | None
    content_ref: str | None
    role_key: str | None = None
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _kind(value: Any, *, allowed: frozenset[str], field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", f"unsupported {field}: {normalized!r}")
    return normalized


def _material_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > 160:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "material_id is invalid")
    return normalized


def register_material(
    state: MutableMapping[str, Any],
    *,
    source_kind: str,
    material_type: str,
    version: str = "v1",
    content: str | None = None,
    content_ref: str | None = None,
    mime_type: str = "text/plain",
    role_key: str | None = None,
    material_id: str | None = None,
) -> dict[str, Any]:
    task_id = str(state.get("task_id") or "").strip()
    if not task_id:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "task_id is required")
    source = _kind(source_kind, allowed=SOURCE_KINDS, field="source_kind")
    material = _kind(material_type, allowed=MATERIAL_TYPES, field="material_type")
    identifier = _material_id(material_id or f"mat-{uuid.uuid4().hex[:20]}")
    encoded = content.encode("utf-8") if content is not None else b""
    record = {
        "material_id": identifier,
        "task_id": task_id,
        "role_key": str(role_key).strip().upper() if role_key else None,
        "source_kind": source,
        "material_type": material,
        "version": str(version or "v1"),
        "mime_type": str(mime_type or "text/plain"),
        "content_length": len(encoded),
        "content_sha256": hashlib.sha256(encoded).hexdigest() if content is not None else None,
        "content": content,
        "content_ref": str(content_ref).strip() if content_ref else None,
    }
    materials = state.setdefault("materials", {})
    if not isinstance(materials, dict):
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "materials registry is not an object")
    existing = materials.get(identifier)
    if existing is not None:
        if json.dumps(existing, sort_keys=True, default=str) != json.dumps(record, sort_keys=True, default=str):
            raise MaterialRefError("MATERIAL_ID_CONFLICT", "material_id already belongs to different metadata")
        return dict(existing)
    materials[identifier] = record
    return dict(record)


def bounded_material_payload(
    *,
    task_id: str,
    material_id: str,
    locator: Mapping[str, Any],
    content: bytes | str | None,
    max_bytes: int = DEFAULT_INLINE_LIMIT,
) -> dict[str, Any]:
    source = _kind(locator.get("source_kind", "inline"), allowed=SOURCE_KINDS, field="source_kind")
    material = _kind(locator.get("material_type", "report"), allowed=MATERIAL_TYPES, field="material_type")
    identifier = _material_id(material_id)
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise MaterialRefError("MATERIAL_METADATA_INVALID", "max_bytes must be positive")
    if content is None:
        return MaterialRef(
            material_id=identifier,
            task_id=task_id,
            role_key=(str(locator.get("role_key") or "").strip().upper() or None),
            source_kind=source,
            material_type=material,
            version=str(locator.get("version") or "v1"),
            mime_type=str(locator.get("mime_type") or "text/plain"),
            content_length=int(locator.get("content_length") or locator.get("size") or 0),
            truncated=False,
            content=None,
            content_ref=(str(locator.get("content_ref") or "").strip() or None),
            error={
                "code": "MATERIAL_CONTENT_UNAVAILABLE",
                "message": "Material content is unavailable for bounded inline display.",
            },
        ).to_dict()
    raw = content if isinstance(content, bytes) else str(content).encode("utf-8")
    bounded = raw[:max_bytes]
    text = bounded.decode("utf-8", errors="replace")
    return MaterialRef(
        material_id=identifier,
        task_id=task_id,
        role_key=(str(locator.get("role_key") or "").strip().upper() or None),
        source_kind=source,
        material_type=material,
        version=str(locator.get("version") or "v1"),
        mime_type=str(locator.get("mime_type") or "text/plain"),
        content_length=len(raw),
        truncated=len(raw) > max_bytes,
        content=text,
        content_ref=(str(locator.get("content_ref") or "").strip() or None),
        error=None,
    ).to_dict()


def missing_material_payload(*, task_id: str, material_id: str) -> dict[str, Any]:
    return MaterialRef(
        material_id=_material_id(material_id),
        task_id=str(task_id),
        role_key=None,
        source_kind="inline",
        material_type="report",
        version="v1",
        mime_type="text/plain",
        content_length=0,
        truncated=False,
        content=None,
        content_ref=None,
        error={"code": "MATERIAL_NOT_FOUND", "message": "Material does not exist."},
    ).to_dict()


def registered_file_content(
    task_private: Mapping[str, Any], locator: Mapping[str, Any]
) -> bytes:
    """Dereference one already-registered repository-relative file material safely."""
    if str(locator.get("source_kind") or "") != "file":
        raise MaterialRefError(
            "MATERIAL_CONTENT_UNAVAILABLE",
            "Only registered file materials may dereference content_ref.",
        )
    repository = Path(str(task_private.get("repository") or "")).expanduser()
    content_ref = str(locator.get("content_ref") or "").strip()
    relative = Path(content_ref)
    if not repository.is_absolute() or not content_ref or relative.is_absolute() or ".." in relative.parts:
        raise MaterialRefError("MATERIAL_REF_UNSAFE", "Registered material reference is unsafe.")
    root = repository.resolve()
    candidate = root.joinpath(relative)
    if candidate.is_symlink():
        raise MaterialRefError("MATERIAL_REF_UNSAFE", "Registered material reference is a symlink.")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise MaterialRefError("MATERIAL_REF_UNSAFE", "Registered material escapes its repository.") from exc
    if not resolved.is_file():
        raise MaterialRefError("MATERIAL_CONTENT_UNAVAILABLE", "Registered material file is unavailable.")
    try:
        return resolved.read_bytes()
    except OSError as exc:
        raise MaterialRefError("MATERIAL_CONTENT_UNAVAILABLE", str(exc)) from exc


def task_context_materials(
    state: Mapping[str, Any],
    tasks: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
) -> dict[str, dict[str, Any]]:
    """Project bounded API-only context without introducing another path owner."""
    task_id = str(state.get("task_id") or "").strip()
    if not task_id:
        return {}
    task_by_id = {str(item.get("task_id") or ""): item for item in tasks}
    dependencies = [
        {
            "task_id": str(dependency_id),
            "status": (
                str(task_by_id[str(dependency_id)].get("status") or "").upper()
                if str(dependency_id) in task_by_id
                else "MISSING"
            ),
        }
        for dependency_id in state.get("depends_on_task_ids") or ()
    ]
    active_hop = next(
        (
            item
            for item in state.get("hops") or ()
            if isinstance(item, Mapping)
            and item.get("hop_id") == state.get("active_hop_id")
        ),
        None,
    )
    report_ids = []
    if isinstance(state.get("materials"), Mapping):
        report_ids = sorted(
            str(material_id)
            for material_id, locator in state["materials"].items()
            if isinstance(locator, Mapping) and locator.get("material_type") == "report"
        )
    context = {
        "task_id": task_id,
        "active_role": state.get("active_role"),
        "effective_goal": state.get("effective_goal") or state.get("task_text"),
        "dependencies": dependencies,
        "prior_report_material_ids": report_ids,
    }
    adopted = state.get("adopted_url_handoff_context")
    if adopted is None:
        adopted = state.get("adopted_url_context")
    if adopted is not None:
        context["adopted_url_handoff_context"] = adopted
    result = {
        "runtime-task-context": _inline_locator(
            task_id,
            "runtime-task-context",
            "task_context",
            json.dumps(context, ensure_ascii=False, sort_keys=True, default=str),
            mime_type="application/json",
        )
    }
    result.update(_repository_materials(state, task_id))
    active_role = str(state.get("active_role") or "").upper()
    workflow_agents = state.get("workflow_agents")
    definition = (
        workflow_agents.get(active_role)
        if active_role and isinstance(workflow_agents, Mapping)
        else None
    )
    if isinstance(definition, Mapping) and str(definition.get("system_prompt") or "").strip():
        result[f"runtime-role-rule-{active_role.lower()}"] = _inline_locator(
            task_id,
            f"runtime-role-rule-{active_role.lower()}",
            "rule",
            str(definition["system_prompt"]),
            role_key=active_role,
        )
    if isinstance(active_hop, Mapping):
        role_key = (
            str(active_hop.get("target_role") or state.get("active_role") or "").upper()
            or None
        )
        prompt = active_hop.get("prompt")
        if isinstance(prompt, str) and prompt:
            result["runtime-current-prompt"] = _inline_locator(
                task_id,
                "runtime-current-prompt",
                "prompt",
                prompt,
                source_kind="generated_prompt",
                role_key=role_key,
            )
        handoff = active_hop.get("handoff")
        if isinstance(handoff, str) and handoff:
            result["runtime-current-handoff"] = _inline_locator(
                task_id,
                "runtime-current-handoff",
                "handoff",
                handoff,
                role_key=role_key,
            )
    return result


def _inline_locator(
    task_id: str,
    material_id: str,
    material_type: str,
    content: str,
    *,
    source_kind: str = "api",
    mime_type: str = "text/plain",
    role_key: str | None = None,
) -> dict[str, Any]:
    encoded = content.encode("utf-8")
    return {
        "material_id": material_id,
        "task_id": task_id,
        "role_key": role_key,
        "source_kind": source_kind,
        "material_type": material_type,
        "version": "v1",
        "mime_type": mime_type,
        "content_length": len(encoded),
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "content": content,
        "content_ref": None,
    }


def _repository_materials(
    state: Mapping[str, Any], task_id: str
) -> dict[str, dict[str, Any]]:
    repository_value = str(state.get("repository") or "").strip()
    if not repository_value:
        return {}
    root = Path(repository_value).expanduser()
    if not root.is_absolute() or not root.is_dir():
        return {}
    root = root.resolve()
    candidates: list[tuple[Path, str]] = []
    project_rule = root / ".agents" / "rules" / "project.md"
    if project_rule.is_file() and not project_rule.is_symlink():
        candidates.append((project_rule, "rule"))
    skills_root = root / ".agents" / "skills"
    if skills_root.is_dir() and not skills_root.is_symlink():
        for skill_path in sorted(skills_root.glob("*/SKILL.md"))[:50]:
            if skill_path.is_file() and not skill_path.is_symlink():
                candidates.append((skill_path, "skill"))
    result: dict[str, dict[str, Any]] = {}
    for path, material_type in candidates:
        try:
            relative = path.resolve().relative_to(root)
            raw = path.read_bytes()
        except (OSError, ValueError):
            continue
        digest = hashlib.sha256(raw).hexdigest()
        material_id = f"runtime-{material_type}-{digest[:20]}"
        result[material_id] = {
            "material_id": material_id,
            "task_id": task_id,
            "role_key": None,
            "source_kind": "file",
            "material_type": material_type,
            "version": f"sha256:{digest}",
            "mime_type": "text/markdown",
            "content_length": len(raw),
            "content_sha256": digest,
            "content": None,
            "content_ref": relative.as_posix(),
        }
    return result
