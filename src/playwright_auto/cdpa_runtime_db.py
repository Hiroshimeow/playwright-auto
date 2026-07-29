from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from .cdpa_projection import TaskProjection

SNAPSHOT_NAMES = frozenset(
    {"catalog", "board", "worker", "browser", "dashboard_actions"}
)
COMMAND_KINDS = frozenset(
    {
        "create_task",
    "change_goal",
        "create_independent_agent",
        "independent_complete",
        "independent_continue",
        "independent_run_now",
        "independent_settings",
        "independent_activate_agent",
        "independent_create_repair",
        "independent_task_control",
        "resume_team",
        "task_control",
        "reload_catalog",
    }
)
COMMAND_STATUSES = frozenset({"queued", "running", "applied", "failed", "recovery_required"})
SCHEMA_VERSION = 1


class RuntimeDBError(RuntimeError):
    pass


class IdempotencyConflict(RuntimeDBError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Mapping[str, Any] | Sequence[Any]) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("value is not JSON serializable") from exc


def _parse(value: str) -> Any:
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeDBError("runtime database contains malformed JSON") from exc


class RuntimeDB:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    def _open_connection(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is not None:
            return connection
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        # Checkpoint each small transaction so heartbeat frames reuse the bounded
        # WAL instead of growing until SQLite's much larger default threshold.
        connection.execute("PRAGMA wal_autocheckpoint=1")
        self._connection = connection
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        # Keep one process-local connection open. Reopening a WAL database for
        # every read recreates the 32 KiB shared-memory file and turns harmless
        # polling into sustained physical writes. The instance lock also makes
        # the shared connection safe for the API's request and telemetry threads.
        with self._lock:
            connection = self._open_connection()
            try:
                yield connection
            except BaseException:
                if connection.in_transaction:
                    connection.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is None:
                return
            if connection.in_transaction:
                connection.rollback()
            connection.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    def ensure_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        bootstrap = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            bootstrap.execute("PRAGMA journal_mode=WAL")
        finally:
            bootstrap.close()
        with self.connection() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, SCHEMA_VERSION}:
                raise RuntimeDBError(
                    f"unsupported runtime database schema version {version}"
                )
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            allowed = {"runtime_snapshot", "task_projection", "command_queue"}
            if existing - allowed:
                raise RuntimeDBError(
                    f"runtime database contains incompatible tables: {sorted(existing - allowed)!r}"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runtime_snapshot (
                    name TEXT PRIMARY KEY,
                    version INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_projection (
                    task_id TEXT PRIMARY KEY,
                    team TEXT NOT NULL,
                    status TEXT NOT NULL,
                    surface TEXT NOT NULL,
                    active_role TEXT,
                    updated_at TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    summary_json TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    private_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS task_projection_surface_updated
                ON task_projection(surface, updated_at DESC, task_id);

                CREATE INDEX IF NOT EXISTS task_projection_status_updated
                ON task_projection(status, updated_at DESC, task_id);

                CREATE TABLE IF NOT EXISTS command_queue (
                    command_id TEXT PRIMARY KEY,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    task_id TEXT,
                    status TEXT NOT NULL,
                    expected_task_version INTEGER,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    payload_json TEXT NOT NULL,
                    result_json TEXT,
                    error TEXT
                );

                CREATE INDEX IF NOT EXISTS command_queue_status_created
                ON command_queue(status, created_at, command_id);
                """
            )
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def put_snapshot(self, name: str, payload: Mapping[str, Any]) -> int:
        if name not in SNAPSHOT_NAMES:
            raise ValueError(f"unsupported runtime snapshot {name!r}")
        encoded = _json(payload)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version, payload_json FROM runtime_snapshot WHERE name=?", (name,)
            ).fetchone()
            if row is not None and row["payload_json"] == encoded:
                connection.commit()
                return int(row["version"])
            version = int(row["version"]) + 1 if row is not None else 1
            connection.execute(
                """
                INSERT INTO runtime_snapshot(name, version, updated_at, payload_json)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    version=excluded.version,
                    updated_at=excluded.updated_at,
                    payload_json=excluded.payload_json
                """,
                (name, version, _now(), encoded),
            )
            connection.commit()
            return version

    def get_snapshot(self, name: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT name, version, updated_at, payload_json FROM runtime_snapshot WHERE name=?",
                (name,),
            ).fetchone()
        if row is None:
            return None
        payload = _parse(row["payload_json"])
        if not isinstance(payload, dict):
            raise RuntimeDBError(f"runtime snapshot {name!r} is not an object")
        return {
            "name": row["name"],
            "version": int(row["version"]),
            "updated_at": row["updated_at"],
            "payload": payload,
        }

    def get_snapshot_version(self, name: str) -> int | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT version FROM runtime_snapshot WHERE name=?", (name,)
            ).fetchone()
        return int(row[0]) if row is not None else None

    @staticmethod
    def _normalized_projection_json(projection: TaskProjection, version: int) -> tuple[str, str, str]:
        summary = dict(projection.summary)
        detail = dict(projection.detail)
        summary["version"] = version
        detail["version"] = version
        return _json(summary), _json(detail), _json(projection.private)

    @staticmethod
    def _same_projection(row: sqlite3.Row, projection: TaskProjection) -> bool:
        summary = dict(projection.summary)
        detail = dict(projection.detail)
        summary["version"] = int(row["version"])
        detail["version"] = int(row["version"])
        return (
            row["team"] == projection.team
            and row["status"] == projection.status
            and row["surface"] == projection.surface
            and row["active_role"] == projection.active_role
            and row["updated_at"] == projection.updated_at
            and row["summary_json"] == _json(summary)
            and row["detail_json"] == _json(detail)
            and row["private_json"] == _json(projection.private)
        )

    def _board_version_unlocked(self, connection: sqlite3.Connection) -> int:
        row = connection.execute(
            "SELECT version FROM runtime_snapshot WHERE name='board'"
        ).fetchone()
        return int(row[0]) if row is not None else 0

    def _write_board_unlocked(self, connection: sqlite3.Connection, version: int) -> None:
        counts = {
            str(row["status"]): int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM task_projection GROUP BY status"
            )
        }
        payload = {"generation": version, "counts": counts}
        connection.execute(
            """
            INSERT INTO runtime_snapshot(name, version, updated_at, payload_json)
            VALUES('board', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                version=excluded.version,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (version, _now(), _json(payload)),
        )

    def _put_catalog_unlocked(self, connection: sqlite3.Connection, catalog: Mapping[str, Any]) -> None:
        encoded = _json(catalog)
        row = connection.execute(
            "SELECT version, payload_json FROM runtime_snapshot WHERE name='catalog'"
        ).fetchone()
        if row is not None and row["payload_json"] == encoded:
            return
        version = int(row["version"]) + 1 if row is not None else 1
        connection.execute(
            """
            INSERT INTO runtime_snapshot(name, version, updated_at, payload_json)
            VALUES('catalog', ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                version=excluded.version,
                updated_at=excluded.updated_at,
                payload_json=excluded.payload_json
            """,
            (version, _now(), encoded),
        )

    def _upsert_unlocked(
        self,
        connection: sqlite3.Connection,
        projections: Sequence[TaskProjection],
    ) -> bool:
        changed = False
        for projection in projections:
            row = connection.execute(
                "SELECT * FROM task_projection WHERE task_id=?", (projection.task_id,)
            ).fetchone()
            if row is not None and self._same_projection(row, projection):
                continue
            version = int(row["version"]) + 1 if row is not None else 1
            summary_json, detail_json, private_json = self._normalized_projection_json(
                projection, version
            )
            connection.execute(
                """
                INSERT INTO task_projection(
                    task_id, team, status, surface, active_role, updated_at, version,
                    summary_json, detail_json, private_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    team=excluded.team,
                    status=excluded.status,
                    surface=excluded.surface,
                    active_role=excluded.active_role,
                    updated_at=excluded.updated_at,
                    version=excluded.version,
                    summary_json=excluded.summary_json,
                    detail_json=excluded.detail_json,
                    private_json=excluded.private_json
                """,
                (
                    projection.task_id,
                    projection.team,
                    projection.status,
                    projection.surface,
                    projection.active_role,
                    projection.updated_at,
                    version,
                    summary_json,
                    detail_json,
                    private_json,
                ),
            )
            changed = True
        return changed

    def replace_task_projections(
        self,
        projections: Sequence[TaskProjection],
        *,
        catalog: Mapping[str, Any],
    ) -> int:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._put_catalog_unlocked(connection, catalog)
            current_board = self._board_version_unlocked(connection)
            if catalog.get("complete") is not True:
                connection.commit()
                return current_board
            changed = self._upsert_unlocked(connection, projections)
            task_ids = {projection.task_id for projection in projections}
            rows = connection.execute("SELECT task_id FROM task_projection").fetchall()
            stale = [row[0] for row in rows if row[0] not in task_ids]
            if stale:
                connection.executemany(
                    "DELETE FROM task_projection WHERE task_id=?",
                    [(task_id,) for task_id in stale],
                )
                changed = True
            if changed or current_board == 0:
                current_board += 1
                self._write_board_unlocked(connection, current_board)
            connection.commit()
            return current_board

    def upsert_task_projections(self, projections: Sequence[TaskProjection]) -> int:
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            generation = self._board_version_unlocked(connection)
            changed = self._upsert_unlocked(connection, projections)
            if changed:
                generation += 1
                self._write_board_unlocked(connection, generation)
            connection.commit()
            return generation

    def list_task_summaries(
        self,
        *,
        surfaces: Sequence[str],
        limit: int,
        cursor: str | None = None,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        conditions: list[str] = []
        values: list[Any] = []
        if surfaces:
            placeholders = ",".join("?" for _ in surfaces)
            conditions.append(f"surface IN ({placeholders})")
            values.extend(surfaces)
        if cursor:
            try:
                updated_at, task_id = cursor.split("\0", 1)
            except ValueError as exc:
                raise ValueError("invalid cursor") from exc
            conditions.append("(updated_at < ? OR (updated_at = ? AND task_id < ?))")
            values.extend((updated_at, updated_at, task_id))
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        values.append(limit)
        with self.connection() as connection:
            rows = connection.execute(
                f"SELECT summary_json FROM task_projection {where} "
                "ORDER BY updated_at DESC, task_id DESC LIMIT ?",
                values,
            ).fetchall()
        return [self._object(row[0], "task summary") for row in rows]

    def list_history(self, *, limit: int, cursor: str | None = None) -> list[dict[str, Any]]:
        return self.list_task_summaries(surfaces=("history",), limit=limit, cursor=cursor)

    @staticmethod
    def _object(value: str, label: str) -> dict[str, Any]:
        parsed = _parse(value)
        if not isinstance(parsed, dict):
            raise RuntimeDBError(f"{label} is not an object")
        return parsed

    def get_task_version(self, task_id: str) -> int | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT version FROM task_projection WHERE task_id=?", (task_id,)
            ).fetchone()
        return int(row[0]) if row is not None else None

    def get_task_detail(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT detail_json FROM task_projection WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._object(row[0], "task detail") if row is not None else None

    def get_task_private(self, task_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT private_json FROM task_projection WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._object(row[0], "task private projection") if row is not None else None

    def get_board(self) -> dict[str, Any]:
        board = self.get_snapshot("board")
        generation = int(board["version"]) if board else 0
        counts = dict((board or {}).get("payload", {}).get("counts", {}))
        catalog = self.get_snapshot("catalog")
        catalog_payload = (catalog or {}).get("payload") or {
            "complete": False,
            "discovered_at": None,
            "errors": ["catalog has not been hydrated"],
        }
        public_errors = []
        for item in catalog_payload.get("errors") or []:
            if isinstance(item, Mapping):
                error = str(item.get("error") or "CatalogError").split(":", 1)[0]
                manifest = Path(str(item.get("manifest") or "")).name
                public_errors.append(
                    {
                        **({"manifest": manifest} if manifest else {}),
                        "error": error or "CatalogError",
                    }
                )
            else:
                public_errors.append({"error": "CatalogError"})
        return {
            "generation": generation,
            "items": self.list_task_summaries(
                surfaces=("active", "offline_recoverable", "history"), limit=1000
            ),
            "counts": counts,
            "catalog": {
                "complete": catalog_payload.get("complete") is True,
                "discovered_at": catalog_payload.get("discovered_at"),
                "errors": public_errors,
            },
        }

    def enqueue_command(
        self,
        *,
        command_id: str,
        idempotency_key: str,
        kind: str,
        task_id: str | None,
        expected_task_version: int | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if kind not in COMMAND_KINDS:
            raise ValueError(f"unsupported command kind {kind!r}")
        if not idempotency_key.strip():
            raise ValueError("idempotency key is required")
        encoded = _json(payload)
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM command_queue WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing is not None:
                same = (
                    existing["kind"] == kind
                    and existing["task_id"] == task_id
                    and existing["expected_task_version"] == expected_task_version
                    and existing["payload_json"] == encoded
                )
                connection.commit()
                if not same:
                    raise IdempotencyConflict(
                        "idempotency key was already used for a different command"
                    )
                return self._command_row(existing)
            connection.execute(
                """
                INSERT INTO command_queue(
                    command_id, idempotency_key, kind, task_id, status,
                    expected_task_version, created_at, payload_json
                ) VALUES(?, ?, ?, ?, 'queued', ?, ?, ?)
                """,
                (
                    command_id,
                    idempotency_key,
                    kind,
                    task_id,
                    expected_task_version,
                    _now(),
                    encoded,
                ),
            )
            row = connection.execute(
                "SELECT * FROM command_queue WHERE command_id=?", (command_id,)
            ).fetchone()
            connection.commit()
        assert row is not None
        return self._command_row(row)

    def claim_next_command(
        self, *, kinds: Sequence[str] | None = None
    ) -> dict[str, Any] | None:
        selected_kinds = tuple(dict.fromkeys(str(kind) for kind in (kinds or ())))
        if selected_kinds and any(kind not in COMMAND_KINDS for kind in selected_kinds):
            raise ValueError("unsupported command kind filter")
        where = "status='queued'"
        values: tuple[str, ...] = ()
        if selected_kinds:
            placeholders = ",".join("?" for _ in selected_kinds)
            where += f" AND kind IN ({placeholders})"
            values = selected_kinds
        # The idle path is read-only. Do not acquire a WAL write lock when the
        # mailbox is empty; that was the dominant source of idle disk writes.
        with self.connection() as connection:
            queued = connection.execute(
                f"SELECT 1 FROM command_queue WHERE {where} LIMIT 1", values
            ).fetchone()
        if queued is None:
            return None
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                f"SELECT command_id FROM command_queue WHERE {where} "
                "ORDER BY created_at, command_id LIMIT 1",
                values,
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            command_id = row[0]
            connection.execute(
                "UPDATE command_queue SET status='running', started_at=? WHERE command_id=? AND status='queued'",
                (_now(), command_id),
            )
            claimed = connection.execute(
                "SELECT * FROM command_queue WHERE command_id=?", (command_id,)
            ).fetchone()
            connection.commit()
        return self._command_row(claimed) if claimed is not None else None

    def finish_command(
        self,
        command_id: str,
        *,
        result: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        status = "failed" if error is not None else "applied"
        result_json = _json(result or {}) if result is not None else None
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE command_queue SET status=?, finished_at=?, result_json=?, error=? "
                "WHERE command_id=?",
                (status, _now(), result_json, error, command_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(command_id)


    def require_command_recovery(
        self,
        command_id: str,
        *,
        error: str,
        result: Mapping[str, Any] | None = None,
    ) -> None:
        result_json = _json(result or {}) if result is not None else None
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE command_queue SET status='recovery_required', finished_at=?, "
                "result_json=?, error=? WHERE command_id=?",
                (_now(), result_json, str(error), command_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(command_id)

    def get_command_by_idempotency_key(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM command_queue WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
        return self._command_row(row) if row is not None else None

    def get_command(self, command_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM command_queue WHERE command_id=?", (command_id,)
            ).fetchone()
        return self._command_row(row) if row is not None else None

    def requeue_running_commands(self) -> int:
        with self.connection() as connection:
            cursor = connection.execute(
                "UPDATE command_queue SET status='queued', started_at=NULL "
                "WHERE status='running'"
            )
            return int(cursor.rowcount)

    @staticmethod
    def _command_row(row: sqlite3.Row) -> dict[str, Any]:
        status = str(row["status"])
        if status not in COMMAND_STATUSES:
            raise RuntimeDBError(f"invalid command status {status!r}")
        payload = _parse(row["payload_json"])
        result = _parse(row["result_json"]) if row["result_json"] else None
        if not isinstance(payload, dict) or (result is not None and not isinstance(result, dict)):
            raise RuntimeDBError("command JSON must be an object")
        return {
            "command_id": row["command_id"],
            "idempotency_key": row["idempotency_key"],
            "kind": row["kind"],
            "task_id": row["task_id"],
            "status": status,
            "expected_task_version": row["expected_task_version"],
            "created_at": row["created_at"],
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
            "payload": payload,
            "result": result,
            "error": row["error"],
        }
