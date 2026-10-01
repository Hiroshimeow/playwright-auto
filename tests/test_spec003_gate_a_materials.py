from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

from playwright_auto.cdpa_config import load_cdpa_config
from playwright_auto.cdpa_materials import register_material
from playwright_auto.cdpa_projection import TaskProjection, build_task_projection
from playwright_auto.cdpa_runtime_db import RuntimeDB
from playwright_auto.cdpa_store import TaskStore
from playwright_auto.dashboard_api import create_server
from test_cdpa_core import write_config
from test_dashboard_api import request


def _projection(tmp_path: Path, *, materials=None, reports=None, private_reports=None) -> TaskProjection:
    summary = {
        "task_id": "task-a",
        "team": "alpha",
        "status": "RUNNING",
        "surface": "active",
        "updated_at": "2026-10-01T00:00:00+00:00",
        "version": 0,
    }
    return TaskProjection(
        task_id="task-a",
        team="alpha",
        status="RUNNING",
        surface="active",
        active_role="DEV",
        updated_at=summary["updated_at"],
        summary=summary,
        detail={
            **summary,
            "timeline": [],
            "reports": list(reports or []),
            "materials": [],
        },
        private={
            "manifest_path": str(tmp_path / ".plan" / "alpha" / "task-a" / "task-a.json"),
            "repository": str(tmp_path),
            "reports": dict(private_reports or {}),
            "materials": dict(materials or {}),
            "maintenance_reports": {},
            "timeline": [],
        },
    )


def _start(tmp_path: Path, projection: TaskProjection):
    config = load_cdpa_config(None, repository_root=tmp_path)
    db = RuntimeDB(config.runtime_database)
    db.ensure_schema()
    db.replace_task_projections(
        [projection],
        catalog={
            "complete": True,
            "discovered_at": "2026-10-01T00:00:00+00:00",
            "errors": [],
            "control_repository": str(tmp_path),
        },
    )
    server = create_server(config, host="127.0.0.1", port=0, db=db)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return db, server, thread


def _get_json(server, path: str) -> dict:
    status, headers, body = request(server, "GET", path)
    assert status == 200
    assert "application/json" in headers.get("Content-Type", "")
    return json.loads(body)


def test_t020_material_report_api_supports_all_gate_a_source_kinds_and_bounds_content(tmp_path) -> None:
    state = {"task_id": "task-a"}
    kinds = (
        "inline",
        "api",
        "generated_prompt",
        "route_json",
        "provider_result",
        "transcript",
    )
    for kind in kinds:
        register_material(
            state,
            source_kind=kind,
            material_type="handoff" if kind == "route_json" else "report",
            version=f"{kind}-v1",
            content=f"content:{kind}",
            mime_type="application/json" if kind in {"route_json", "provider_result"} else "text/plain",
            role_key="DEV",
            material_id=f"mat-{kind}",
        )
    register_material(
        state,
        source_kind="transcript",
        material_type="report",
        content="x" * 70000,
        role_key="DEV",
        material_id="mat-large",
    )

    db, server, thread = _start(tmp_path, _projection(tmp_path, materials=state["materials"]))
    try:
        for kind in kinds:
            payload = _get_json(server, f"/api/report/task-a/mat-{kind}")
            assert payload["source_kind"] == kind
            assert payload["version"] == f"{kind}-v1"
            assert payload["content"] == f"content:{kind}"
            assert payload["content_length"] == len(f"content:{kind}".encode())
            assert payload["truncated"] is False
            assert payload["error"] is None

        large = _get_json(server, "/api/report/task-a/mat-large")
        assert large["content_length"] == 70000
        assert large["truncated"] is True
        assert len(large["content"].encode()) <= 64 * 1024
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()


def test_t020_file_report_bridge_and_structured_missing_unsafe_errors(tmp_path) -> None:
    report = tmp_path / ".plan" / "alpha" / "task-a" / "dev.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("file-backed report", encoding="utf-8")
    body = report.read_bytes()
    report_locator = {
        "path": str(report),
        "sha256": hashlib.sha256(body).hexdigest(),
        "size": len(body),
        "repository": str(tmp_path),
        "team": "alpha",
        "availability": "available",
    }

    secret = tmp_path / "outside-material.txt"
    secret.write_text("DO_NOT_DEREFERENCE", encoding="utf-8")
    state = {"task_id": "task-a"}
    register_material(
        state,
        source_kind="generated_prompt",
        material_type="handoff",
        content=None,
        content_ref=str(secret),
        role_key="DEV",
        material_id="mat-opaque-ref",
    )

    projection = _projection(
        tmp_path,
        materials=state["materials"],
        reports=[{"report_id": "r1", "url": "/api/reports/task-a/r1"}],
        private_reports={"r1": report_locator},
    )
    db, server, thread = _start(tmp_path, projection)
    try:
        file_payload = _get_json(server, "/api/report/task-a/r1")
        assert file_payload["source_kind"] == "file"
        assert file_payload["material_type"] == "report"
        assert file_payload["content"] == "file-backed report"
        assert file_payload["error"] is None

        opaque = _get_json(server, "/api/report/task-a/mat-opaque-ref")
        assert opaque["content"] is None
        assert opaque["content_ref"] == str(secret)
        assert opaque["error"]["code"] == "MATERIAL_CONTENT_UNAVAILABLE"
        assert "DO_NOT_DEREFERENCE" not in json.dumps(opaque)

        missing = _get_json(server, "/api/report/task-a/mat-missing")
        assert missing["content"] is None
        assert missing["error"]["code"] == "MATERIAL_NOT_FOUND"
        assert missing["error"]["message"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        db.close()


def test_t021_projection_and_dashboard_use_material_backed_nonblank_handoff(tmp_path) -> None:
    config = load_cdpa_config(write_config(tmp_path), repository_root=tmp_path)
    store = TaskStore(config)
    state = store.create_task(
        "material handoff",
        requested_team="material-handoff",
        task_id="task-material-handoff",
        roles=("DEV",),
        closing_role="DEV",
    )
    register_material(
        state,
        source_kind="route_json",
        material_type="handoff",
        version="route-contract-test",
        content='{"route":"DONE","handoff":"complete"}',
        role_key="DEV",
        material_id="handoff-hop-1",
    )

    projection = build_task_projection(state, tasks=[state])
    material_row = next(
        item for item in projection.detail["reports"] if item.get("material_id") == "handoff-hop-1"
    )
    assert material_row["material_url"] == "/api/report/task-material-handoff/handoff-hop-1"
    assert material_row["availability"] == "available"
    assert material_row["role"] == "DEV"
    assert projection.summary["has_reports"] is True

    app = Path("src/playwright_auto/dashboard_assets/app.js").read_text(encoding="utf-8")
    detail = Path("src/playwright_auto/dashboard_assets/views/task_detail.js").read_text(encoding="utf-8")
    assert "const endpoint = report.material_url || report.url;" in app
    assert "const payload = await response.json();" in app
    assert "payload.content || errorText || \"Material content is unavailable for bounded inline display.\"" in app
    assert "function reportSelectionKey(report)" in detail
    assert "report?.url || report?.material_url || null" in detail
    assert "if (loaded?.status === \"ready\") return loaded.body;" in detail
