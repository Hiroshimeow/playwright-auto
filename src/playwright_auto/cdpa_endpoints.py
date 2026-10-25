from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit
from uuid import uuid4

from .connection import is_local_cdp_endpoint, normalize_cdp_endpoint
from .file_lock import exclusive_file_lock, fsync_parent_directory

_ENDPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", re.ASCII)
_ENDPOINT_FIELDS = frozenset({
    "endpoint_id", "cdp_url", "label", "is_default", "temporary_default",
    "management_mode", "profile_path", "executable_path", "rate_limit_scope",
    "max_active_tabs", "draining", "removed",
    # Accepted only so older registry records remain readable; ignored below.
    "tool_capabilities", "tool_capabilities_configured", "tool_capability_bindings",
})


def normalize_endpoint_record(raw: Mapping[str, Any], *, endpoint_id: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) - _ENDPOINT_FIELDS:
        raise ValueError("browser endpoint contains unsupported fields")
    if not isinstance(endpoint_id, str) or not _ENDPOINT_ID.fullmatch(endpoint_id):
        raise ValueError("invalid browser endpoint_id")
    address = normalize_cdp_endpoint(raw.get("cdp_url"))
    result: dict[str, Any] = {"endpoint_id": endpoint_id, "cdp_url": address}
    for name in ("is_default", "temporary_default", "draining", "removed"):
        value = raw.get(name, False)
        if type(value) is not bool:
            raise ValueError(f"browser {name} must be a boolean")
        result[name] = value
    capacity = raw.get("max_active_tabs", 2)
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValueError("browser max_active_tabs must be a positive integer")
    result["max_active_tabs"] = capacity
    for name, default in (("label", address), ("rate_limit_scope", f"endpoint:{endpoint_id}")):
        value = raw.get(name, default)
        if not isinstance(value, str) or not value.strip() or len(value) > 256:
            raise ValueError(f"browser {name} must contain 1-256 characters")
        if any(ord(char) < 32 for char in value):
            raise ValueError(f"browser {name} must not contain control characters")
        result[name] = value.strip()
    mode = raw.get("management_mode", "external")
    if mode not in {"external", "managed-local"}:
        raise ValueError("management_mode must be external or managed-local")
    if mode == "managed-local" and not is_local_cdp_endpoint(address):
        raise ValueError("only local endpoints can have a managed process")
    if mode == "managed-local":
        parsed = urlsplit(address)
        if parsed.scheme != "http" or parsed.port is None:
            raise ValueError("managed-local browser requires a loopback HTTP endpoint with an explicit port")
    result["management_mode"] = mode
    for name in ("profile_path", "executable_path"):
        value = raw.get(name)
        if value is not None:
            if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
                raise ValueError(f"browser {name} must be an absolute path")
            path = Path(value).expanduser()
            if name == "profile_path" and any(
                part.is_symlink() or getattr(part, "is_junction", lambda: False)()
                for part in (path, *path.parents)
            ):
                raise ValueError("profile paths must not traverse a symlink or junction")
            value = str(path.resolve())
        result[name] = value
    if mode == "managed-local" and not result["profile_path"]:
        raise ValueError("managed-local browser requires a profile_path")
    if result["is_default"] and (result["draining"] or result["removed"]):
        raise ValueError("a draining or removed browser cannot be default")
    return result


def endpoint_config_seeds(browser: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Validate seed configuration; generated identities are persisted by the registry."""
    raw = browser.get("endpoints")
    if raw is None:
        raw = [{
            "endpoint_id": "legacy",
            "cdp_url": browser.get("cdp_url", "http://127.0.0.1:9222"),
            "is_default": True,
            "temporary_default": browser.get("temporary_default", False),
        }]
    if not isinstance(raw, list) or not raw:
        raise ValueError("browser.endpoints must be a non-empty array")
    seeds = []
    for index, value in enumerate(raw):
        if not isinstance(value, Mapping):
            raise ValueError("browser endpoint must be an object")
        identity = value.get("endpoint_id", f"seed-{index}")
        record = normalize_endpoint_record(value, endpoint_id=identity)
        if "endpoint_id" not in value:
            record.pop("endpoint_id")
            if "rate_limit_scope" not in value:
                record.pop("rate_limit_scope")  # derive from the final durable ID at seeding
        seeds.append(record)
    identities = [item["endpoint_id"] for item in seeds if "endpoint_id" in item]
    if len(identities) != len(set(identities)):
        raise ValueError("duplicate browser endpoint_id")
    if len({item["cdp_url"] for item in seeds}) != len(seeds):
        raise ValueError("duplicate browser endpoint address")
    defaults = sum(item["is_default"] for item in seeds)
    if defaults > 1:
        raise ValueError("only one browser can be default")
    if defaults == 0:
        candidate = next((item for item in seeds if not item["draining"] and not item["removed"]), None)
        if candidate is not None:
            candidate["is_default"] = True
    return tuple(seeds)


class BrowserEndpointRegistry:
    """Durable configuration catalog, using the same atomic-file discipline as bootstraps.

    Health is a worker projection, not a persisted assertion of readiness. Removed
    records remain tombstones so old manifests can never acquire a new default.
    """

    def __init__(self, config: Any) -> None:
        self.config = config
        self.path = config.repository_root / ".runtime" / "cdpa-browser-endpoints.json"
        self.lock_path = self.path.with_suffix(".json.lock")

    def _seed(self) -> dict[str, Any]:
        seeds = getattr(self.config, "browser_endpoints", ()) or endpoint_config_seeds({"cdp_url": self.config.cdp_url})
        entries = {}
        for seed in seeds:
            identity = seed.get("endpoint_id") or str(uuid4())
            entries[identity] = normalize_endpoint_record(seed, endpoint_id=identity)
        old_address = normalize_cdp_endpoint(self.config.cdp_url)
        legacy = next((key for key, item in entries.items() if item["cdp_url"] == old_address), None)
        if legacy is None:
            # A registry-only configuration has no prior legacy browser. Its initial
            # default is the explicit compatibility binding, never the later default.
            legacy = next((key for key, item in entries.items() if item["is_default"]), next(iter(entries)))
        return {"version": 1, "legacy_endpoint_id": legacy, "entries": entries}

    @staticmethod
    def _validate(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict) or set(raw) != {"version", "legacy_endpoint_id", "entries"}:
            raise ValueError("invalid browser endpoint registry")
        if type(raw["version"]) is not int or raw["version"] != 1 or not isinstance(raw["entries"], dict):
            raise ValueError("unsupported browser endpoint registry version")
        entries = {}
        for identity, item in raw["entries"].items():
            if not isinstance(item, Mapping) or item.get("endpoint_id") != identity:
                raise ValueError("browser endpoint key/identity mismatch")
            entries[identity] = normalize_endpoint_record(item, endpoint_id=identity)
        if raw["legacy_endpoint_id"] not in entries:
            raise ValueError("browser registry has lost its legacy binding")
        active = [item for item in entries.values() if not item["removed"]]
        if len({item["cdp_url"] for item in active}) != len(active):
            raise ValueError("browser endpoint address is already registered")
        eligible = [item for item in active if not item["draining"]]
        if sum(item["is_default"] for item in active) != int(bool(eligible)):
            raise ValueError("browser registry must have exactly one eligible default")
        return {"version": 1, "legacy_endpoint_id": raw["legacy_endpoint_id"], "entries": entries}

    def _read_existing(self) -> dict[str, Any]:
        def unique_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate key in browser registry")
                result[key] = value
            return result
        return self._validate(json.loads(self.path.read_text(encoding="utf-8"), object_pairs_hook=unique_pairs))

    def snapshot(self) -> dict[str, Any]:
        if self.path.exists():
            return self._read_existing()
        with exclusive_file_lock(self.lock_path):
            if self.path.exists():
                return self._read_existing()
            initial = self._validate(self._seed())
            self._write(initial)
            return initial

    @property
    def legacy_endpoint_id(self) -> str:
        return self.snapshot()["legacy_endpoint_id"]

    def list(self, *, include_removed: bool = False) -> list[dict[str, Any]]:
        return [item for item in self.snapshot()["entries"].values() if include_removed or not item["removed"]]

    def get(self, endpoint_id: str) -> dict[str, Any]:
        item = self.snapshot()["entries"].get(endpoint_id)
        if item is None:
            raise ValueError(f"unknown browser endpoint: {endpoint_id}")
        return item

    def resolve(self, value: str | None = None) -> dict[str, Any]:
        catalog = self.snapshot()
        entries = catalog["entries"]
        if value is None:
            item = next((entry for entry in entries.values() if entry["is_default"]), None)
        elif value in entries:
            item = entries[value]
        else:
            try:
                address = normalize_cdp_endpoint(value)
            except ValueError as exc:
                raise ValueError(f"browser endpoint is not registered: {value}") from exc
            item = next((entry for entry in entries.values() if entry["cdp_url"] == address and not entry["removed"]), None)
        if item is None:
            raise ValueError("browser endpoint is not registered or no default is available")
        if item["draining"] or item["removed"]:
            raise ValueError("browser endpoint is draining or removed")
        return item

    def add(self, address: str, **settings: Any) -> dict[str, Any]:
        self.snapshot()
        normalized_address = normalize_cdp_endpoint(address)
        requested_identity = settings.pop("endpoint_id", None)
        with exclusive_file_lock(self.lock_path):
            catalog = self._read_existing()
            duplicate = next(
                (
                    item
                    for item in catalog["entries"].values()
                    if not item["removed"] and item["cdp_url"] == normalized_address
                ),
                None,
            )
            if duplicate is not None:
                if requested_identity not in {None, duplicate["endpoint_id"]}:
                    raise ValueError(
                        "browser endpoint address is already registered with conflicting identity"
                    )
                candidate = normalize_endpoint_record(
                    {**duplicate, **settings, "cdp_url": normalized_address},
                    endpoint_id=duplicate["endpoint_id"],
                )
                if candidate == duplicate:
                    return duplicate
                raise ValueError(
                    "browser endpoint address is already registered with different settings"
                )
            identity = requested_identity or str(uuid4())
            if identity in catalog["entries"]:
                raise ValueError("browser endpoint_id is already registered")
            settings.setdefault(
                "is_default",
                not any(item["is_default"] for item in catalog["entries"].values()),
            )
            item = normalize_endpoint_record(
                {**settings, "cdp_url": normalized_address}, endpoint_id=identity
            )
            if item["is_default"]:
                for other in catalog["entries"].values():
                    other["is_default"] = False
            catalog["entries"][identity] = item
            self._write(catalog)
            return item

    def update(self, endpoint_id: str, **changes: Any) -> dict[str, Any]:
        if set(changes) - (_ENDPOINT_FIELDS - {"endpoint_id", "removed"}):
            raise ValueError("unsupported browser endpoint update")
        if "draining" in changes and changes["draining"] is not False:
            raise ValueError("browser endpoint settings may only clear draining; use Remove to drain")
        self.snapshot()
        with exclusive_file_lock(self.lock_path):
            catalog = self._read_existing()
            existing = catalog["entries"].get(endpoint_id)
            if existing is None or existing["removed"]:
                raise ValueError("unknown or removed browser endpoint")
            updated = normalize_endpoint_record(
                {**existing, **changes}, endpoint_id=endpoint_id
            )
            if updated == existing:
                return existing
            if updated["is_default"]:
                for other in catalog["entries"].values():
                    other["is_default"] = False
            catalog["entries"][endpoint_id] = updated
            self._write(catalog)
            return updated

    def remove(self, endpoint_id: str, *, has_nonterminal_tasks: bool) -> dict[str, Any]:
        self.snapshot()
        with exclusive_file_lock(self.lock_path):
            catalog = self._read_existing()
            item = catalog["entries"].get(endpoint_id)
            if item is None:
                raise ValueError("unknown browser endpoint")
            item.update(draining=True, removed=not has_nonterminal_tasks, is_default=False)
            eligible = [other for other in catalog["entries"].values() if not other["removed"] and not other["draining"]]
            if eligible and not any(other["is_default"] for other in eligible):
                eligible[0]["is_default"] = True
            self._write(catalog)
            return item

    def _write(self, value: Mapping[str, Any]) -> None:
        normalized = self._validate(value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=self.path.parent,
                                             prefix=self.path.name + ".", suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(normalized, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            fsync_parent_directory(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
