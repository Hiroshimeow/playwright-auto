from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .config import RuntimeConfig


class EndpointInventoryError(RuntimeError):
    """Raised when endpoint inventory config cannot be parsed safely."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass(frozen=True)
class EndpointInventoryResult:
    items: list[dict[str, Any]]
    diagnostics: list[dict[str, str]]


@dataclass
class EndpointInventory:
    """Read-only owner of endpoint config and reachability summaries."""

    config: RuntimeConfig

    def summarize_endpoints(self, *, probe: bool = False) -> EndpointInventoryResult:
        records, diagnostics = self._load_endpoint_records()
        checked_at = datetime.now(timezone.utc).isoformat()
        items = [
            self._summarize_endpoint_record(record, checked_at=checked_at, probe=probe)
            for record in records
        ]
        return EndpointInventoryResult(items=items, diagnostics=diagnostics)

    def _load_endpoint_records(self) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        if self.config.endpoint_registry_path.exists():
            return self._load_registry_records(self.config.endpoint_registry_path), []
        if self.config.endpoint_config_path.exists():
            return self._load_config_seed_records(self.config.endpoint_config_path), []
        return [], [
            {
                "code": "endpoint_config_missing",
                "message": "No cdpa.yaml or endpoint registry was found for read-only inventory.",
            }
        ]

    def _load_registry_records(self, path: Path) -> list[dict[str, Any]]:
        value = self._read_json_file(path)
        if not isinstance(value, dict) or not isinstance(value.get("entries"), dict):
            raise EndpointInventoryError(
                "endpoint_config_invalid",
                "endpoint registry must contain an entries object",
            )
        records: list[dict[str, Any]] = []
        for endpoint_id, raw_record in value["entries"].items():
            if not isinstance(raw_record, dict):
                raise EndpointInventoryError(
                    "endpoint_config_invalid",
                    "endpoint registry record must be an object",
                )
            if raw_record.get("removed"):
                continue
            records.append(self._normalize_record(raw_record, endpoint_id=str(endpoint_id), source=path))
        return records

    def _load_config_seed_records(self, path: Path) -> list[dict[str, Any]]:
        value = self._read_json_file(path)
        if not isinstance(value, dict) or not isinstance(value.get("browser"), dict):
            raise EndpointInventoryError(
                "endpoint_config_invalid",
                "cdpa.yaml must contain a browser object",
            )
        browser = value["browser"]
        raw_endpoints = browser.get("endpoints")
        if raw_endpoints is None:
            record = {
                "endpoint_id": "legacy",
                "label": "legacy",
                "cdp_url": browser.get("cdp_url", "http://127.0.0.1:9222"),
            }
            return [self._normalize_record(record, endpoint_id="legacy", source=path)]
        if not isinstance(raw_endpoints, list):
            raise EndpointInventoryError(
                "endpoint_config_invalid",
                "browser.endpoints must be a list",
            )
        records: list[dict[str, Any]] = []
        for index, raw_record in enumerate(raw_endpoints):
            if not isinstance(raw_record, dict):
                raise EndpointInventoryError(
                    "endpoint_config_invalid",
                    "browser endpoint record must be an object",
                )
            endpoint_id = str(raw_record.get("endpoint_id") or f"seed-{index}")
            records.append(self._normalize_record(raw_record, endpoint_id=endpoint_id, source=path))
        return records

    def _read_json_file(self, path: Path) -> Any:
        try:
            raw = path.read_text(encoding="utf-8-sig")
            return json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise EndpointInventoryError("endpoint_config_invalid", str(exc)) from exc

    def _normalize_record(
        self,
        raw_record: dict[str, Any],
        *,
        endpoint_id: str,
        source: Path,
    ) -> dict[str, Any]:
        cdp_url = str(raw_record.get("cdp_url") or "").strip()
        if not cdp_url:
            raise EndpointInventoryError(
                "endpoint_config_invalid",
                "endpoint record must contain cdp_url",
            )
        return {
            "endpoint_id": endpoint_id,
            "label": str(raw_record.get("label") or endpoint_id),
            "cdp_url": cdp_url,
            "source": source.name,
        }

    def _summarize_endpoint_record(
        self,
        record: dict[str, Any],
        *,
        checked_at: str,
        probe: bool,
    ) -> dict[str, Any]:
        reachable = False
        diagnostic = "not_probed"
        if probe:
            reachable = self._probe_cdp_url(str(record["cdp_url"]))
            diagnostic = "ok" if reachable else "endpoint_probe_failed"
        return {
            "endpoint_id": record["endpoint_id"],
            "label": record["label"],
            "cdp_url": record["cdp_url"],
            "reachable": reachable,
            "source": record["source"],
            "mode": "read_only_inventory",
            "last_checked_at": checked_at,
            "diagnostic": diagnostic,
        }

    def _probe_cdp_url(self, cdp_url: str) -> bool:
        parsed = urlsplit(cdp_url)
        if parsed.scheme not in {"http", "https", "ws", "wss"}:
            return False
        if not parsed.hostname or parsed.port is None:
            return False
        return self._probe_host_port(
            parsed.hostname,
            parsed.port,
            self.config.endpoint_probe_timeout_seconds,
        )

    def _probe_host_port(self, host: str, port: int, timeout: float) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False


