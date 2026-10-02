#!/usr/bin/env python3
"""Create one idempotent CDPA Solo Task via the local control API."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROLES = ("PLAN", "DEV", "REVIEW", "TEST", "AUDIT")


def build_payload(args: argparse.Namespace) -> dict[str, str]:
    payload: dict[str, str] = {
        "workflow_mode": "solo",
        "role": args.role.upper(),
        "task": args.task.strip(),
        "goal": args.goal.strip(),
        "requested_team": args.team.strip(),
        "browser_endpoint_id": args.browser.strip(),
        "repository": str(Path(args.repository).expanduser().resolve()),
    }
    if args.conversation_url:
        payload["conversation_url"] = args.conversation_url.strip()
    return payload


def canonical_json(payload: dict[str, str]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def idempotency_key(payload: dict[str, str], explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:32]
    return f"solo-{payload['role'].lower()}-{digest}"


def post_json(url: str, payload: dict[str, str], key: str, timeout: float) -> dict:
    data = canonical_json(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": key,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"CDPA API HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"CDPA API unavailable: {exc.reason}") from exc
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed.get("task_id"):
        raise RuntimeError(f"CDPA API returned no task_id: {parsed!r}")
    return parsed


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--role", required=True, choices=ROLES)
    p.add_argument("--task", required=True)
    p.add_argument("--goal", required=True)
    p.add_argument("--team", required=True)
    p.add_argument("--browser", required=True, dest="browser")
    p.add_argument("--repository", required=True)
    p.add_argument("--conversation-url")
    p.add_argument("--idempotency-key")
    p.add_argument("--api-base", default="http://127.0.0.1:9225")
    p.add_argument("--timeout", type=float, default=15.0)
    p.add_argument("--dry-run", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    payload = build_payload(args)
    key = idempotency_key(payload, args.idempotency_key)
    if args.dry_run:
        print(json.dumps({"payload": payload, "idempotency_key": key}, ensure_ascii=False, indent=2))
        return 0
    url = f"{args.api_base.rstrip('/')}/api/tasks"
    result = post_json(url, payload, key, args.timeout)
    print(
        json.dumps(
            {
                "task_id": result.get("task_id"),
                "command_id": result.get("command_id"),
                "status": result.get("status"),
                "team": payload["requested_team"],
                "role": payload["role"],
                "idempotency_key": key,
                "browser_endpoint_id": payload["browser_endpoint_id"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
