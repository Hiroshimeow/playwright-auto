from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

_REDACTED = "[REDACTED]"
_URL_PATTERN = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_SENSITIVE_QUERY_TERMS = (
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "credential",
    "signature",
    "signed",
    "session",
    "jwt",
    "ticket",
    "code",
    "key",
)
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?:bearer|basic|token|secret|password|passwd|credential|signature|api[-_]?key|session|jwt)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"\b(Bearer|Basic)\s+[^\s,;]+", re.IGNORECASE)
_ASSIGNMENT_PATTERN = re.compile(
    r"\b(authorization|access[_-]?token|refresh[_-]?token|id[_-]?token|api[_-]?key|apikey|"
    r"password|passwd|secret|token|credential|signature|session(?:[_-]?id)?|jwt)\b"
    r"(\s*[:=]\s*)([^\s,;]+)",
    re.IGNORECASE,
)
_PATH_SECRET_MARKERS = frozenset(
    {
        "auth",
        "authorization",
        "capability",
        "code",
        "credential",
        "credentials",
        "hook",
        "hooks",
        "invite",
        "invitation",
        "magic-link",
        "oauth",
        "jwt",
        "key",
        "keys",
        "password-reset",
        "reset",
        "reset-password",
        "secret",
        "secrets",
        "session",
        "sessions",
        "signature",
        "signed",
        "activation",
        "verification",
        "verify",
        "token",
        "tokens",
        "webhook",
        "webhooks",
    }
)
_PATH_SECRET_COMPACT_COMPONENT_PAIRS = frozenset(
    {
        ("access", "token"),
        ("activation", "code"),
        ("api", "key"),
        ("api", "token"),
        ("auth", "token"),
        ("bearer", "token"),
        ("client", "credential"),
        ("client", "secret"),
        ("csrf", "token"),
        ("id", "token"),
        ("invite", "code"),
        ("magic", "link"),
        ("password", "reset"),
        ("refresh", "token"),
        ("reset", "code"),
        ("reset", "password"),
        ("session", "id"),
        ("session", "token"),
        ("signed", "url"),
        ("verification", "code"),
    }
)
_PATH_SECRET_COMPACT_CORES = frozenset(
    left + right for left, right in _PATH_SECRET_COMPACT_COMPONENT_PAIRS
) | frozenset({"authorization", "oauth", "oauth2", "webhook", "webhooks"})
_PATH_SECRET_COMPACT_SUFFIX_RULES = {
    "callback": frozenset(
        {
            "authorization",
            "magiclink",
            "oauth",
            "oauth2",
            "signedurl",
            "webhook",
            "webhooks",
        }
    ),
    "incoming": frozenset({"webhook", "webhooks"}),
    "link": frozenset({"passwordreset"}),
    "redirect": frozenset({"authorization", "oauth", "oauth2"}),
}
_PATH_SECRET_COMPACT_ALIASES = frozenset(
    set(_PATH_SECRET_COMPACT_CORES)
    | {
        base + suffix
        for suffix, bases in _PATH_SECRET_COMPACT_SUFFIX_RULES.items()
        for base in bases
    }
)
_JWT_PATH_SEGMENT = re.compile(
    r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?$"
)


def _sensitive_query(name: str, value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(name).casefold()).strip("_")
    return any(term in normalized for term in _SENSITIVE_QUERY_TERMS) or bool(
        _SENSITIVE_VALUE_PATTERN.search(str(value))
    )


def _normalized_path_segment(value: str) -> str:
    decoded = unquote(value)
    decoded = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "-", decoded)
    decoded = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", "-", decoded)
    return re.sub(r"[^a-z0-9]+", "-", decoded.casefold()).strip("-")


def _opaque_path_secret(value: str) -> bool:
    return bool(_JWT_PATH_SEGMENT.fullmatch(unquote(value)))


def _high_risk_path_marker(value: str) -> bool:
    compact = value.replace("-", "")
    if compact in _PATH_SECRET_COMPACT_ALIASES:
        return True
    padded = f"-{value}-"
    return any(f"-{marker}-" in padded for marker in _PATH_SECRET_MARKERS)


def _sensitive_path_indexes(path: str) -> tuple[set[int], bool]:
    parts = path.split("/")
    sensitive: set[int] = set()
    high_risk_context = False
    for index, raw in enumerate(parts):
        if not raw:
            continue
        normalized = _normalized_path_segment(raw)
        if high_risk_context:
            sensitive.add(index)
            continue
        if _high_risk_path_marker(normalized):
            sensitive.add(index)
            high_risk_context = True
            continue
        decoded = unquote(raw)
        if (
            _SENSITIVE_VALUE_PATTERN.search(decoded)
            or _ASSIGNMENT_PATTERN.search(decoded)
            or _opaque_path_secret(raw)
        ):
            sensitive.add(index)
    return sensitive, high_risk_context


def _sanitize_path(path: str) -> tuple[str, bool]:
    value = path or "/"
    parts = value.split("/")
    sensitive, high_risk_context = _sensitive_path_indexes(value)
    for index in sensitive:
        parts[index] = _REDACTED
    return "/".join(parts) or "/", bool(sensitive or high_risk_context)


def _split_url(value: str):
    try:
        parsed = urlsplit(str(value).strip())
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return parsed, netloc


def sanitize_url(value: str | None) -> str | None:
    if not value:
        return None
    split = _split_url(str(value))
    if split is None:
        return None
    parsed, netloc = split
    pairs = []
    for name, item in parse_qsl(parsed.query, keep_blank_values=True):
        pairs.append((name, _REDACTED if _sensitive_query(name, item) else item))
    query = urlencode(pairs, doseq=True)
    path, _path_redacted = _sanitize_path(parsed.path or "/")
    return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def probeable_url(value: str | None) -> str | None:
    if not value:
        return None
    split = _split_url(str(value))
    if split is None:
        return None
    parsed, netloc = split
    if parsed.username is not None or parsed.password is not None:
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(_sensitive_query(name, item) for name, item in pairs):
        return None
    path, path_redacted = _sanitize_path(parsed.path or "/")
    if path_redacted:
        return None
    query = urlencode(pairs, doseq=True)
    return urlunsplit((parsed.scheme.casefold(), netloc, path, query, ""))


def extract_probeable_url(detail: str | None) -> str | None:
    if not detail:
        return None
    match = _URL_PATTERN.search(str(detail))
    if not match:
        return None
    candidate = match.group(0).rstrip(".,);]")
    return probeable_url(candidate)


def sanitize_text(value: Any, *, max_chars: int | None = None) -> str:
    text = str(value or "")
    safe_urls: list[str] = []

    def replace_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,);]":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        safe = (sanitize_url(raw) or "[REDACTED_URL]") + suffix
        index = len(safe_urls)
        safe_urls.append(safe)
        return f"__CDPA_SAFE_URL_{index}__"

    text = _URL_PATTERN.sub(replace_url, text)
    text = _BEARER_PATTERN.sub(lambda match: f"{match.group(1)} {_REDACTED}", text)
    text = _ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}", text
    )
    for index, safe in enumerate(safe_urls):
        text = text.replace(f"__CDPA_SAFE_URL_{index}__", safe)
    if max_chars is not None and len(text) > max_chars:
        suffix = "…[TRUNCATED]"
        text = text[: max(0, max_chars - len(suffix))] + suffix
    return text


def sanitize_exception(error: BaseException, *, max_chars: int = 2000) -> str:
    return sanitize_text(f"{type(error).__name__}: {error}", max_chars=max_chars)


def sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): sanitize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_value(item) for item in value]
    if isinstance(value, str):
        return sanitize_text(value)
    return value


def project_environment_signature(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    scalar_keys = (
        "version",
        "prerequisite",
        "health_probe",
        "available",
        "probe_available",
        "failure_observed",
        "browser_available",
        "browser_is_connected",
        "browser_live_operation",
        "browser_live_succeeded",
        "repository_available",
        "plans_available",
        "network_available",
        "tooling_probe_supported",
        "tooling_dependency_identity",
        "tooling_endpoint",
        "tooling_capability_succeeded",
        "tooling_tool_count",
        "tooling_auth_profile",
        "tooling_credential_resolved",
        "tooling_catalog_sha256",
        "tooling_protocol_version",
        "tooling_session_mode",
        "tooling_session_id_sha256",
        "tooling_cleanup_attempted",
        "tooling_cleanup_succeeded",
        "filesystem_write_fsync_delete",
    )
    result = {key: sanitize_value(value.get(key)) for key in scalar_keys if key in value}
    for key in (
        "browser_error",
        "browser_live_error",
        "tooling_error",
        "filesystem_error",
    ):
        if key in value:
            result[key] = sanitize_text(value.get(key), max_chars=2000) or None
    pages = []
    for page in value.get("browser_pages") or ():
        if isinstance(page, Mapping):
            pages.append(
                {
                    "page_id": str(page.get("page_id") or "") or None,
                    "url": sanitize_url(str(page.get("url") or "")) if page.get("url") else None,
                }
            )
    if pages:
        result["browser_pages"] = pages
    evidence = value.get("network_evidence")
    if isinstance(evidence, Mapping):
        result["network_evidence"] = {
            "endpoint": sanitize_url(str(evidence.get("endpoint") or ""))
            if evidence.get("endpoint")
            else None,
            "method": str(evidence.get("method") or "") or None,
            "timeout_seconds": evidence.get("timeout_seconds"),
            "status": evidence.get("status"),
            "error": sanitize_text(evidence.get("error"), max_chars=2000) or None,
        }
    elif "network_evidence" in value:
        result["network_evidence"] = None
    if "network_errors" in value:
        result["network_errors"] = [
            sanitize_text(item, max_chars=2000) for item in value.get("network_errors") or ()
        ]
    for key in (
        "tooling_required_tools",
        "tooling_matched_tools",
        "tooling_missing_tools",
    ):
        if key in value:
            result[key] = [str(item) for item in value.get(key) or ()]
    descriptor = value.get("tooling_probe_descriptor")
    if isinstance(descriptor, Mapping):
        result["tooling_probe_descriptor"] = {
            key: sanitize_value(descriptor.get(key))
            for key in (
                "version",
                "kind",
                "dependency",
                "endpoint",
                "method",
                "auth_profile",
                "required_tools",
            )
            if key in descriptor
        }
    tooling_evidence = value.get("tooling_evidence")
    if isinstance(tooling_evidence, Mapping):
        steps = []
        for step in tooling_evidence.get("steps") or ():
            if isinstance(step, Mapping):
                steps.append(
                    {
                        key: (
                            sanitize_text(step.get(key), max_chars=2000)
                            if key == "error"
                            else sanitize_value(step.get(key))
                        )
                        for key in (
                            "stage",
                            "http_method",
                            "rpc_method",
                            "status",
                            "content_type",
                            "response_bytes",
                            "error",
                        )
                        if key in step
                    }
                )
        result["tooling_evidence"] = {"steps": steps}
    return result


def project_maintenance_incident(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    scalar_keys = (
        "incident_id",
        "task_id",
        "team",
        "state",
        "turn",
        "trigger_status",
        "trigger_code",
        "source_hop_id",
        "source_role",
        "created_at",
        "updated_at",
        "resolved_at",
        "request_id",
        "report_path",
        "report_sha256",
        "report_size",
        "environment_attempts",
        "environment_prerequisite",
        "environment_last_attempt_at",
        "environment_suspended_at",
        "environment_last_probe_at",
        "environment_resumed_at",
        "repair_task_id",
        "repair_team",
        "repair_disposition",
        "control_id",
        "command_index",
        "lesson_append_state",
        "lesson_finalized_at",
    )
    result = {key: sanitize_value(value.get(key)) for key in scalar_keys if key in value}
    for key in ("trigger_reason", "environment_last_error", "last_error"):
        if key in value:
            result[key] = sanitize_text(value.get(key), max_chars=2000) or None
    control_ids = value.get("control_ids")
    if isinstance(control_ids, (list, tuple)):
        result["control_ids"] = [str(item) for item in control_ids]
    for key in (
        "environment_signature",
        "environment_resume_signature",
        "environment_last_probe_signature",
    ):
        projected = project_environment_signature(value.get(key))
        if projected is not None:
            result[key] = projected
    decision = value.get("decision")
    if isinstance(decision, Mapping):
        decision_summary: dict[str, Any] = {
            "action": str(decision.get("action") or "") or None,
            "role": str(decision.get("role") or "") or None,
            "version": decision.get("version"),
        }
        recovery = []
        for step in decision.get("recovery") or ():
            if isinstance(step, Mapping):
                recovery.append(
                    {
                        "action": str(step.get("action") or "") or None,
                        "role": str(step.get("role") or "") or None,
                        "reason": sanitize_text(step.get("reason"), max_chars=1000) or None,
                    }
                )
        if recovery:
            decision_summary["recovery"] = recovery
        repair = decision.get("repair")
        if isinstance(repair, Mapping):
            decision_summary["repair"] = {
                "disposition": str(repair.get("disposition") or "") or None,
                "root_cause": sanitize_text(repair.get("root_cause"), max_chars=1200) or None,
            }
        result["decision"] = decision_summary
    return result
