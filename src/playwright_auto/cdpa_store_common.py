from __future__ import annotations

import re
from datetime import datetime, timezone

from .cdpa_identity import validate_task_id

TERMINAL = frozenset({"DONE", "STOPPED"})


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str, *, maximum: int = 72) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    return (slug or "task")[:maximum].rstrip("-")


def _validate_task_id(value: str) -> str:
    return validate_task_id(value)
