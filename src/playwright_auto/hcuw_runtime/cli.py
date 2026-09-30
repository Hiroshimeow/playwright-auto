from __future__ import annotations

import argparse
from typing import Sequence

from .api_server import RuntimeApiServer, RuntimeApiServerError
from .audit import AuditTimeline
from .config import ConfigError, load_runtime_config
from .dashboard_server import DashboardServer, DashboardServerError
from .endpoint_inventory import EndpointInventory
from .legacy_audit import LegacyObservationAudit
from .state import RuntimeStateStore
from .task_runner import RuntimeTaskRunner


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_runtime_config(repository_root=args.repository)
        if args.command == "start-api":
            _start_api(config)
            return 0
        if args.command == "start-dashboard":
            _start_dashboard(config)
            return 0
    except (ConfigError, RuntimeApiServerError, DashboardServerError) as exc:
        parser.exit(status=2, message=f"{exc}\n")
    parser.error("unknown command")
    return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hcuw-runtime")
    parser.add_argument(
        "--repository",
        default=None,
        help="Repository root. Defaults to the current working directory.",
    )
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("start-api", help="Start the runtime-v2 API on 9227")
    subcommands.add_parser(
        "start-dashboard",
        help="Start the runtime-v2 dashboard on 9226",
    )
    return parser


def _start_api(config) -> None:
    state_store = RuntimeStateStore(config)
    state_store.initialize()
    inventory = EndpointInventory(config)
    audit_timeline = AuditTimeline()
    task_runner = RuntimeTaskRunner(config, state_store, audit_timeline)
    legacy_audit = LegacyObservationAudit(config)
    RuntimeApiServer(
        config,
        state_store,
        inventory,
        audit_timeline,
        task_runner,
        legacy_audit,
    ).serve_forever()


def _start_dashboard(config) -> None:
    DashboardServer(config).serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
