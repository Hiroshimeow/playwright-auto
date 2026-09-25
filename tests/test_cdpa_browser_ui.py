from __future__ import annotations

import json
import subprocess
from playwright_auto.dashboard import ASSET_ROOT


def test_temporary_creation_inherits_selected_browser_and_force_only_enables():
    module = (ASSET_ROOT / "views/browsers.js").resolve().as_uri()
    script = f'''
      import {{creationUsesTemporary}} from {json.dumps(module)};
      import assert from 'node:assert/strict';
      const state = {{runtime: {{browser: {{default_endpoint_id: 'b', endpoints: [
        {{endpoint_id: 'a', temporary_default: false}},
        {{endpoint_id: 'b', temporary_default: true}},
      ]}}}}}};
      assert.equal(creationUsesTemporary(state, '', false), true);
      assert.equal(creationUsesTemporary(state, 'a', false), false);
      assert.equal(creationUsesTemporary(state, 'a', true), true);
      assert.equal(creationUsesTemporary(state, 'b', false), true);
    '''
    subprocess.run(["node", "--input-type=module", "-e", script], check=True, capture_output=True, text=True)


def test_browser_ui_exposes_execution_entries_and_preserves_dirty_fields():
    browsers = (ASSET_ROOT / "views/browsers.js").read_text(encoding="utf-8")
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views/task_detail.js").read_text(encoding="utf-8")
    assert "endpoint.running_executions" in browsers
    assert "execution.team" in browsers and "execution.role" in browsers
    assert "process_error" in browsers
    assert "form.dataset.dirty" in browsers
    assert "creationUsesTemporary" in app
    assert "taskId: `browser:${endpointId}`" in app
    assert "last_endpoint_id" in detail


def test_changed_browser_module_graph_uses_fresh_cache_identity_for_changed_modules():
    from playwright_auto.dashboard import DASHBOARD_HTML_PATH

    topology_key = "?v=20260914-goal2-ops-v1"
    board_key = "?v=20260915-operator-ux-v1"
    browsers_key = "?v=20260915-endpoint-all-v1"
    live_key = "?v=20260925-live-audit-ux-v3"
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    notify = (ASSET_ROOT / "views/notify.js").read_text(encoding="utf-8")
    board = (ASSET_ROOT / "views/board.js").read_text(encoding="utf-8")
    browsers = (ASSET_ROOT / "views/browsers.js").read_text(encoding="utf-8")
    detail = (ASSET_ROOT / "views/task_detail.js").read_text(encoding="utf-8")

    assert f'"./views/board.js{board_key}"' in app
    assert f'"./views/browsers.js{browsers_key}"' in app
    assert f'"./views/task_detail.js{live_key}"' in app
    assert f'"./views/notify.js{live_key}"' in app
    assert f'"./task_detail.js{live_key}"' in notify
    for source in (board, browsers, detail):
        assert f'"./topology.js{topology_key}"' in source
    assert f'/assets/app.js{live_key}' in html
    assert f'/assets/dashboard.css{live_key}' in html
