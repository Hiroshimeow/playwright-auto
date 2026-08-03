from __future__ import annotations

import hashlib
import re
import time
from datetime import datetime, timezone

_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def generate_task_id(task: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    digest = hashlib.sha256(f"{task}\0{time.time_ns()}".encode()).hexdigest()[:8]
    return f"cdpa-{stamp}-{digest}"


def generate_idempotent_task_id(task: str, idempotency_key: str) -> str:
    """Derive one opaque task identity for one API idempotency reservation."""
    key = str(idempotency_key).strip()
    if not key:
        raise ValueError("idempotency key is required")
    digest = hashlib.sha256(f"{key}\0{task}".encode()).hexdigest()[:24]
    return f"cdpa-idem-{digest}"


def validate_task_id(value: str) -> str:
    task_id = str(value).strip()
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError("task_id must match [A-Za-z0-9][A-Za-z0-9._-]{0,127}")
    return task_id
