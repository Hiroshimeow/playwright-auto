"""Worker adapters for tests that exercise RoleController with a lightweight worker."""
from types import SimpleNamespace

from playwright_auto.cdpa_worker import CDPAWorker


class RoleRuntimeTestWorker(SimpleNamespace):
    _empty_terminal_repair_parent = staticmethod(CDPAWorker._empty_terminal_repair_parent)
    _idle_report_repair_parent = CDPAWorker._idle_report_repair_parent
