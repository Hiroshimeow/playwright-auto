from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .connection import validate_cdp_url

CDPA_ROLES = ("PLAN", "DEV", "REVIEW", "TEST", "AUDIT")
DEFAULTS_ROOT = Path(__file__).with_name("cdpa_defaults")
DEFAULT_CONFIG_PATH = DEFAULTS_ROOT / "cdpa.json"
CDPA_DELAY_ACTIONS = frozenset(
    {
        "composer_fill",
        "send",
        "new_chat",
        "refresh",
        "open_tab",
        "close_tab",
        "delete_dialog",
        "delete_confirm",
    }
)


class CDPAConfigError(ValueError):
    pass


CDPA_TOOLING_AUTH_PROFILES = frozenset({"none", "local_mcp_static_bearer"})
_TOOLING_PROBE_KEYS = frozenset({"dependency", "endpoint", "auth_profile", "required_tools"})


@dataclass(frozen=True)
class CDPAToolingProbeConfig:
    dependency: str
    endpoint: str
    auth_profile: str
    required_tools: tuple[str, ...]


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CDPAConfigError(f"{name} must be an object")
    return value


def _positive(value: Any, name: str, *, integer: bool = False) -> float | int:
    try:
        parsed = int(value) if integer else float(value)
    except (TypeError, ValueError) as exc:
        raise CDPAConfigError(f"{name} must be numeric") from exc
    if parsed <= 0:
        raise CDPAConfigError(f"{name} must be positive")
    return parsed


def _resolve(root: Path, value: Any, name: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise CDPAConfigError(f"{name} must not be empty")
    path = Path(text).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


def _tooling_probe_config(value: Any) -> CDPAToolingProbeConfig | None:
    if value is None:
        return None
    raw = _mapping(value, "maintenance.tooling_probe")
    if set(raw) != _TOOLING_PROBE_KEYS:
        raise CDPAConfigError(
            "maintenance.tooling_probe must contain exactly "
            f"{sorted(_TOOLING_PROBE_KEYS)!r}"
        )
    dependency = str(raw.get("dependency") or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", dependency) is None:
        raise CDPAConfigError(
            "maintenance.tooling_probe.dependency must be a bounded identifier"
        )
    endpoint = str(raw.get("endpoint") or "").strip()
    try:
        parsed = urlparse(endpoint)
        port = parsed.port
    except ValueError as exc:
        raise CDPAConfigError("maintenance.tooling_probe.endpoint is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or port is None
        or not (1 <= port <= 65535)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise CDPAConfigError(
            "maintenance.tooling_probe.endpoint must be exact loopback HTTP with an explicit port"
        )
    endpoint_path = parsed.path or "/"
    host = f"[{parsed.hostname}]" if parsed.hostname == "::1" else parsed.hostname
    canonical_endpoint = f"http://{host}:{port}{endpoint_path}"
    if endpoint != canonical_endpoint:
        raise CDPAConfigError("maintenance.tooling_probe.endpoint must be canonical")
    auth_profile = str(raw.get("auth_profile") or "").strip()
    if auth_profile not in CDPA_TOOLING_AUTH_PROFILES:
        raise CDPAConfigError(
            "maintenance.tooling_probe.auth_profile is unsupported"
        )
    required_raw = raw.get("required_tools")
    if (
        not isinstance(required_raw, list)
        or not required_raw
        or len(required_raw) > 16
    ):
        raise CDPAConfigError(
            "maintenance.tooling_probe.required_tools must contain 1 to 16 names"
        )
    required_tools = tuple(str(item).strip() for item in required_raw)
    if any(
        re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", item) is None
        for item in required_tools
    ):
        raise CDPAConfigError(
            "maintenance.tooling_probe.required_tools contains an invalid name"
        )
    if len(set(required_tools)) != len(required_tools):
        raise CDPAConfigError(
            "maintenance.tooling_probe.required_tools must be unique"
        )
    return CDPAToolingProbeConfig(
        dependency=dependency,
        endpoint=endpoint,
        auth_profile=auth_profile,
        required_tools=required_tools,
    )


@dataclass(frozen=True)
class CDPAConfig:
    repository_root: Path
    config_path: Path
    plans_root: Path
    constructor_paths: Mapping[str, Path]
    maintainers_constructor_path: Path
    response_guide_path: Path
    roles: tuple[str, ...]
    dashboard_url: str
    dashboard_port: int
    dashboard_poll_seconds: float
    cdp_url: str
    workspace_timeout_seconds: float
    route_repair_attempts: int
    response_timeout_seconds: float
    response_refresh_after_seconds: float
    response_stable_ms: int
    response_poll_ms: int
    maintenance_timeout_seconds: float
    maintenance_refresh_after_seconds: float
    maintenance_stable_ms: int
    maintenance_poll_ms: int
    maintenance_tooling_probe: CDPAToolingProbeConfig | None
    cleanup_terminal_idle_seconds: float
    worker_poll_seconds: float
    delay_minimum_seconds: float
    delay_maximum_seconds: float
    delay_multipliers: Mapping[str, float]


def load_cdpa_config(
    path: str | Path | None = None,
    *,
    repository_root: str | Path | None = None,
) -> CDPAConfig:
    root = Path(repository_root or Path.cwd()).expanduser().resolve()
    if path is None:
        local_config = root / "cdpa.yaml"
        config_path = local_config if local_config.is_file() else DEFAULT_CONFIG_PATH
    else:
        config_path = Path(path).expanduser()
        if not config_path.is_absolute():
            config_path = root / config_path
    config_path = config_path.resolve()
    asset_root = DEFAULTS_ROOT.resolve() if config_path == DEFAULT_CONFIG_PATH.resolve() else root
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CDPAConfigError(f"missing CDPA config: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise CDPAConfigError(
            "cdpa.yaml must use JSON-compatible YAML syntax: "
            f"line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    root_value = _mapping(raw, "cdpa.yaml")
    paths = _mapping(root_value.get("paths"), "paths")
    constructors = _mapping(paths.get("constructors"), "paths.constructors")
    roles_raw = root_value.get("roles", CDPA_ROLES)
    if not isinstance(roles_raw, list):
        raise CDPAConfigError("roles must be an array")
    roles = tuple(str(role).strip().upper() for role in roles_raw)
    if roles != CDPA_ROLES:
        raise CDPAConfigError(f"roles must be exactly {list(CDPA_ROLES)!r}")
    constructor_paths = {
        role: _resolve(asset_root, constructors.get(role), f"paths.constructors.{role}")
        for role in roles
    }
    for role, constructor in constructor_paths.items():
        if not constructor.is_file():
            raise CDPAConfigError(f"constructor prompt for {role} does not exist: {constructor}")
    maintainers_constructor = _resolve(
        asset_root,
        paths.get("maintainers_constructor"),
        "paths.maintainers_constructor",
    )
    if not maintainers_constructor.is_file():
        raise CDPAConfigError(
            f"Maintainers constructor prompt does not exist: {maintainers_constructor}"
        )
    guide = _resolve(asset_root, paths.get("response_guide"), "paths.response_guide")
    if not guide.is_file():
        raise CDPAConfigError(f"response guide does not exist: {guide}")

    dashboard = _mapping(root_value.get("dashboard"), "dashboard")
    browser = _mapping(root_value.get("browser"), "browser")
    repair = _mapping(root_value.get("route_repair"), "route_repair")
    response = _mapping(root_value.get("response"), "response")
    maintenance = _mapping(root_value.get("maintenance"), "maintenance")
    cleanup = _mapping(root_value.get("cleanup"), "cleanup")
    worker = _mapping(root_value.get("worker"), "worker")
    delays = _mapping(root_value.get("delays"), "delays")
    multipliers = _mapping(delays.get("multipliers", {}), "delays.multipliers")

    minimum = float(_positive(delays.get("minimum_seconds", 1.0), "delays.minimum_seconds"))
    maximum = float(_positive(delays.get("maximum_seconds", 1.5), "delays.maximum_seconds"))
    if minimum > maximum:
        raise CDPAConfigError("delays.minimum_seconds must not exceed maximum_seconds")
    unknown_delay_actions = set(map(str, multipliers)) - CDPA_DELAY_ACTIONS
    if unknown_delay_actions:
        raise CDPAConfigError(
            f"unknown delays.multipliers actions: {sorted(unknown_delay_actions)!r}"
        )
    delay_multipliers = {
        str(name): float(_positive(value, f"delays.multipliers.{name}"))
        for name, value in multipliers.items()
    }
    maintenance_tooling_probe = _tooling_probe_config(
        maintenance.get("tooling_probe")
    )
    dashboard_port = int(
        _positive(dashboard.get("port", 9224), "dashboard.port", integer=True)
    )
    dashboard_url = str(dashboard.get("url") or "").rstrip("/")
    try:
        parsed_dashboard_url = urlparse(dashboard_url)
        dashboard_url_port = parsed_dashboard_url.port
    except ValueError as exc:
        raise CDPAConfigError("dashboard.url must be valid loopback HTTP") from exc
    if (
        parsed_dashboard_url.scheme != "http"
        or parsed_dashboard_url.hostname not in {"127.0.0.1", "localhost"}
        or parsed_dashboard_url.username is not None
        or parsed_dashboard_url.password is not None
        or parsed_dashboard_url.path not in {"", "/"}
        or parsed_dashboard_url.params
        or parsed_dashboard_url.query
        or parsed_dashboard_url.fragment
    ):
        raise CDPAConfigError("dashboard.url must be loopback HTTP without a path")
    if dashboard_url_port != dashboard_port:
        raise CDPAConfigError("dashboard.url port must match dashboard.port")

    return CDPAConfig(
        repository_root=root,
        config_path=config_path,
        plans_root=_resolve(root, paths.get("plans_root", ".plan"), "paths.plans_root"),
        constructor_paths=constructor_paths,
        maintainers_constructor_path=maintainers_constructor,
        response_guide_path=guide,
        roles=roles,
        dashboard_url=dashboard_url,
        dashboard_port=dashboard_port,
        dashboard_poll_seconds=float(_positive(dashboard.get("poll_seconds", 1), "dashboard.poll_seconds")),
        cdp_url=validate_cdp_url(str(browser.get("cdp_url", "http://127.0.0.1:9222"))),
        workspace_timeout_seconds=float(_positive(browser.get("workspace_timeout_seconds", 15), "browser.workspace_timeout_seconds")),
        route_repair_attempts=int(_positive(repair.get("max_attempts", 3), "route_repair.max_attempts", integer=True)),
        response_timeout_seconds=float(_positive(response.get("timeout_seconds", 7200), "response.timeout_seconds")),
        response_refresh_after_seconds=float(_positive(response.get("refresh_after_seconds", 1200), "response.refresh_after_seconds")),
        response_stable_ms=int(_positive(response.get("stable_ms", 1000), "response.stable_ms", integer=True)),
        response_poll_ms=int(_positive(response.get("poll_ms", 100), "response.poll_ms", integer=True)),
        maintenance_timeout_seconds=float(
            _positive(maintenance.get("timeout_seconds", 300), "maintenance.timeout_seconds")
        ),
        maintenance_refresh_after_seconds=float(
            _positive(
                maintenance.get("refresh_after_seconds", 120),
                "maintenance.refresh_after_seconds",
            )
        ),
        maintenance_stable_ms=int(
            _positive(maintenance.get("stable_ms", 1000), "maintenance.stable_ms", integer=True)
        ),
        maintenance_poll_ms=int(
            _positive(maintenance.get("poll_ms", 100), "maintenance.poll_ms", integer=True)
        ),
        maintenance_tooling_probe=maintenance_tooling_probe,
        cleanup_terminal_idle_seconds=float(_positive(cleanup.get("terminal_idle_seconds", 3600), "cleanup.terminal_idle_seconds")),
        worker_poll_seconds=float(_positive(worker.get("poll_seconds", 1), "worker.poll_seconds")),
        delay_minimum_seconds=minimum,
        delay_maximum_seconds=maximum,
        delay_multipliers=delay_multipliers,
    )
