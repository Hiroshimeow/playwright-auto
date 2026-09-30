from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__


class ConfigError(ValueError):
    """Raised when runtime-v2 configuration cannot be resolved safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class RuntimeConfig:
    """Resolved runtime-v2 configuration.

    RuntimeConfig owns only configuration facts. It does not create directories,
    open sockets, probe endpoints, or mutate runtime state.
    """

    repository_root: Path
    api_host: str
    api_port: int
    dashboard_host: str
    dashboard_port: int
    legacy_api_port: int
    state_root: Path
    endpoint_config_path: Path
    endpoint_registry_path: Path
    dashboard_min_refresh_seconds: int
    endpoint_probe_timeout_seconds: float
    version: str = __version__

    @property
    def runs_path(self) -> Path:
        return self.state_root / "runs.json"

    @property
    def diagnostics_path(self) -> Path:
        return self.state_root / "diagnostics.jsonl"

    def to_health_payload(
        self,
        *,
        started_at: str,
        branch: str | None = None,
        head: str | None = None,
    ) -> dict[str, Any]:
        return {
            "service": "hcuw-runtime-v2-api",
            "status": "ok",
            "host": self.api_host,
            "port": self.api_port,
            "dashboard_port": self.dashboard_port,
            "state_root": _display_path(self.state_root, self.repository_root),
            "legacy_api_port": self.legacy_api_port,
            "legacy_isolation": True,
            "version": self.version,
            "branch": branch,
            "head": head,
            "started_at": started_at,
        }

    def to_config_payload(self) -> dict[str, Any]:
        return {
            "api": {
                "host": self.api_host,
                "port": self.api_port,
            },
            "dashboard": {
                "host": self.dashboard_host,
                "port": self.dashboard_port,
                "api_proxy_prefix": "/api",
            },
            "state": {
                "root": _display_path(self.state_root, self.repository_root),
                "runs_path": _display_path(self.runs_path, self.repository_root),
                "diagnostics_path": _display_path(
                    self.diagnostics_path,
                    self.repository_root,
                ),
            },
            "legacy": {
                "api_port": self.legacy_api_port,
                "writable_state_shared": False,
            },
            "endpoints": {
                "mode": "read_only_inventory",
                "source": _display_path(self.endpoint_config_path, self.repository_root),
                "registry_source": _display_path(
                    self.endpoint_registry_path,
                    self.repository_root,
                ),
            },
            "limits": {
                "dashboard_min_refresh_seconds": self.dashboard_min_refresh_seconds,
                "endpoint_probe_timeout_seconds": self.endpoint_probe_timeout_seconds,
            },
        }


def load_runtime_config(
    *,
    repository_root: str | Path | None = None,
    api_port: int = 9227,
    dashboard_port: int = 9226,
    legacy_api_port: int = 9225,
    api_host: str = "127.0.0.1",
    dashboard_host: str = "127.0.0.1",
    state_root: str | Path = ".runtime-v2",
    dashboard_min_refresh_seconds: int = 10,
    endpoint_probe_timeout_seconds: float = 2.0,
) -> RuntimeConfig:
    root = _resolve_repository_root(repository_root)
    validated_api_port = _validate_port(api_port, field_name="api_port")
    validated_dashboard_port = _validate_port(
        dashboard_port,
        field_name="dashboard_port",
    )
    validated_legacy_port = _validate_port(
        legacy_api_port,
        field_name="legacy_api_port",
    )
    _validate_api_port_is_not_legacy_port(validated_api_port, validated_legacy_port)
    _validate_dashboard_port_is_not_legacy_port(
        validated_dashboard_port,
        validated_legacy_port,
    )
    resolved_state_root = _resolve_state_root(root, state_root)
    return RuntimeConfig(
        repository_root=root,
        api_host=api_host,
        api_port=validated_api_port,
        dashboard_host=dashboard_host,
        dashboard_port=validated_dashboard_port,
        legacy_api_port=validated_legacy_port,
        state_root=resolved_state_root,
        endpoint_config_path=root / "cdpa.yaml",
        endpoint_registry_path=root / ".runtime" / "cdpa-browser-endpoints.json",
        dashboard_min_refresh_seconds=dashboard_min_refresh_seconds,
        endpoint_probe_timeout_seconds=endpoint_probe_timeout_seconds,
    )


def _resolve_repository_root(repository_root: str | Path | None) -> Path:
    if repository_root is None:
        return Path.cwd().resolve()
    return Path(repository_root).resolve()


def _validate_port(value: int, *, field_name: str) -> int:
    if not isinstance(value, int) or value < 1 or value > 65535:
        raise ConfigError("invalid_port", f"{field_name} must be a TCP port")
    return value


def _validate_api_port_is_not_legacy_port(api_port: int, legacy_api_port: int) -> None:
    if api_port == legacy_api_port:
        raise ConfigError(
            "legacy_port_conflict",
            "runtime-v2 API port must not reuse the legacy API port",
        )


def _validate_dashboard_port_is_not_legacy_port(
    dashboard_port: int,
    legacy_api_port: int,
) -> None:
    if dashboard_port == legacy_api_port:
        raise ConfigError(
            "legacy_port_conflict",
            "runtime-v2 dashboard port must not reuse the legacy API port",
        )


def _resolve_state_root(repository_root: Path, state_root: str | Path) -> Path:
    root = Path(state_root)
    resolved = root if root.is_absolute() else repository_root / root
    resolved = resolved.resolve()
    _validate_state_root_is_runtime_v2(repository_root, resolved)
    return resolved


def _validate_state_root_is_runtime_v2(repository_root: Path, state_root: Path) -> None:
    expected = (repository_root / ".runtime-v2").resolve()
    if state_root != expected:
        raise ConfigError(
            "invalid_state_root",
            "runtime-v2 state root must be exactly .runtime-v2 under the repository",
        )


def _display_path(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return str(path)
