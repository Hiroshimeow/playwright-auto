from __future__ import annotations

import json
import math
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


def _bounded_float(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CDPAConfigError(f"{name} must be numeric") from exc
    if not math.isfinite(parsed) or parsed < minimum or parsed > maximum:
        raise CDPAConfigError(f"{name} must be between {minimum} and {maximum} seconds")
    return parsed


def _resolve(root: Path, value: Any, name: str) -> Path:
    text = str(value or "").strip()
    if not text:
        raise CDPAConfigError(f"{name} must not be empty")
    path = Path(text).expanduser()
    return (path if path.is_absolute() else root / path).resolve()


@dataclass(frozen=True)
class CDPAConfig:
    repository_root: Path
    config_path: Path
    plans_root: Path
    constructor_paths: Mapping[str, Path]
    independent_rule_path: Path
    response_guide_path: Path
    roles: tuple[str, ...]
    dashboard_url: str
    dashboard_port: int
    dashboard_poll_seconds: float
    dashboard_api_host: str
    dashboard_api_port: int
    runtime_database: Path
    worker_stale_seconds: float
    command_poll_seconds: float
    heartbeat_seconds: float
    browser_inventory_seconds: float
    repository_allowed_roots: tuple[Path, ...]
    cdp_url: str
    workspace_timeout_seconds: float
    route_repair_attempts: int
    response_timeout_seconds: float
    response_refresh_after_seconds: float
    response_stream_status_poll_seconds: float
    response_stream_status_terminal_settle_seconds: float
    response_stable_ms: int
    response_poll_ms: int
    independent_seed_builtins: bool
    independent_idle_close_seconds: float
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
    independent_rule = _resolve(
        asset_root,
        paths.get("independent_rule", "prompts/cdpa/INDEPENDENT_RULE.md"),
        "paths.independent_rule",
    )
    if not independent_rule.is_file():
        raise CDPAConfigError(
            f"independent-agent operating rule does not exist: {independent_rule}"
        )
    guide = _resolve(asset_root, paths.get("response_guide"), "paths.response_guide")
    if not guide.is_file():
        raise CDPAConfigError(f"response guide does not exist: {guide}")

    dashboard = _mapping(root_value.get("dashboard"), "dashboard")
    runtime = _mapping(root_value.get("runtime", {}), "runtime")
    repositories = _mapping(root_value.get("repositories", {}), "repositories")
    browser = _mapping(root_value.get("browser"), "browser")
    repair = _mapping(root_value.get("route_repair"), "route_repair")
    response = _mapping(root_value.get("response"), "response")
    cleanup = _mapping(root_value.get("cleanup"), "cleanup")
    independent_agents = _mapping(
        root_value.get("independent_agents", {}), "independent_agents"
    )
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

    dashboard_api_host = str(dashboard.get("api_host", "127.0.0.1")).strip()
    if dashboard_api_host not in {"127.0.0.1", "localhost", "::1"}:
        raise CDPAConfigError("dashboard.api_host must be loopback")
    dashboard_api_port = int(
        _positive(dashboard.get("api_port", 9225), "dashboard.api_port", integer=True)
    )
    if not 1 <= dashboard_api_port <= 65535:
        raise CDPAConfigError("dashboard.api_port must be a valid TCP port")
    if dashboard_api_port == dashboard_port:
        raise CDPAConfigError("dashboard.api_port must differ from dashboard.port")

    runtime_database = _resolve(
        root, runtime.get("database", ".runtime/cdpa-control.sqlite3"), "runtime.database"
    )
    try:
        runtime_database.relative_to(root)
    except ValueError as exc:
        raise CDPAConfigError("runtime.database must remain inside the control repository") from exc

    allowed_raw = repositories.get("allowed_roots", [str(root.parent)])
    if not isinstance(allowed_raw, list) or not allowed_raw:
        raise CDPAConfigError("repositories.allowed_roots must be a non-empty array")
    repository_allowed_roots = tuple(
        _resolve(root, value, "repositories.allowed_roots") for value in allowed_raw
    )

    return CDPAConfig(
        repository_root=root,
        config_path=config_path,
        plans_root=_resolve(root, paths.get("plans_root", ".plan"), "paths.plans_root"),
        constructor_paths=constructor_paths,
        independent_rule_path=independent_rule,
        response_guide_path=guide,
        roles=roles,
        dashboard_url=dashboard_url,
        dashboard_port=dashboard_port,
        dashboard_poll_seconds=float(_positive(dashboard.get("poll_seconds", 1), "dashboard.poll_seconds")),
        dashboard_api_host=dashboard_api_host,
        dashboard_api_port=dashboard_api_port,
        runtime_database=runtime_database,
        worker_stale_seconds=float(
            _positive(runtime.get("worker_stale_seconds", 15), "runtime.worker_stale_seconds")
        ),
        command_poll_seconds=float(
            _positive(runtime.get("command_poll_seconds", 1), "runtime.command_poll_seconds")
        ),
        heartbeat_seconds=float(
            _positive(runtime.get("heartbeat_seconds", 5), "runtime.heartbeat_seconds")
        ),
        browser_inventory_seconds=float(
            _positive(runtime.get("browser_inventory_seconds", 5), "runtime.browser_inventory_seconds")
        ),
        repository_allowed_roots=repository_allowed_roots,
        cdp_url=validate_cdp_url(str(browser.get("cdp_url", "http://127.0.0.1:9222"))),
        workspace_timeout_seconds=float(_positive(browser.get("workspace_timeout_seconds", 15), "browser.workspace_timeout_seconds")),
        route_repair_attempts=int(_positive(repair.get("max_attempts", 3), "route_repair.max_attempts", integer=True)),
        response_timeout_seconds=float(_positive(response.get("timeout_seconds", 7200), "response.timeout_seconds")),
        response_refresh_after_seconds=float(_positive(response.get("refresh_after_seconds", 1200), "response.refresh_after_seconds")),
        response_stream_status_poll_seconds=_bounded_float(
            response.get("stream_status_poll_seconds", 10.0),
            "response.stream_status_poll_seconds",
            minimum=0.2,
            maximum=60.0,
        ),
        response_stream_status_terminal_settle_seconds=_bounded_float(
            response.get("stream_status_terminal_settle_seconds", 5.0),
            "response.stream_status_terminal_settle_seconds",
            minimum=0.0,
            maximum=60.0,
        ),
        response_stable_ms=int(_positive(response.get("stable_ms", 1000), "response.stable_ms", integer=True)),
        response_poll_ms=int(_positive(response.get("poll_ms", 100), "response.poll_ms", integer=True)),
        independent_seed_builtins=bool(
            independent_agents.get("seed_builtins", True)
        ),
        independent_idle_close_seconds=float(
            _positive(
                independent_agents.get("idle_close_seconds", 60),
                "independent_agents.idle_close_seconds",
            )
        ),
        cleanup_terminal_idle_seconds=float(_positive(cleanup.get("terminal_idle_seconds", 300), "cleanup.terminal_idle_seconds")),
        worker_poll_seconds=float(_positive(worker.get("poll_seconds", 1), "worker.poll_seconds")),
        delay_minimum_seconds=minimum,
        delay_maximum_seconds=maximum,
        delay_multipliers=delay_multipliers,
    )
