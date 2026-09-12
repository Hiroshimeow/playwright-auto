import inspect
from pathlib import Path

from playwright_auto.cdpa_worker import CDPAWorker
from playwright_auto.role_runtime.controller import RoleController
from playwright_auto.role_runtime.model import Context
from playwright_auto.role_runtime.page_observer import PageObserver


def test_hybrid_observation_has_no_runtime_dom_only_switch():
    assert not hasattr(CDPAWorker, "_dom_only_enabled")
    assert "dom_only" not in inspect.signature(RoleController.decide).parameters
    assert "dom_only" not in inspect.signature(RoleController.maintain_history).parameters
    assert "dom_only" not in inspect.signature(PageObserver.wait).parameters
    assert "dom_only" not in Context.__dataclass_fields__


def test_dashboard_exposes_no_dom_only_toggle_or_runtime_setting_route():
    root = Path(__file__).resolve().parents[1] / "src" / "playwright_auto"
    html = (root / "dashboard.html").read_text(encoding="utf-8")
    app = (root / "dashboard_assets" / "app.js").read_text(encoding="utf-8")
    runtime = (root / "dashboard_assets" / "views" / "runtime.js").read_text(encoding="utf-8")
    api = (root / "dashboard_api.py").read_text(encoding="utf-8")
    for text in (html, app, runtime, api):
        assert "dom_only" not in text
        assert "data-dom-only" not in text
    assert '"/api/runtime/settings"' not in app
