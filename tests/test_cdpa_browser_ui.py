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


def test_changed_browser_module_graph_uses_one_new_cache_identity():
    from playwright_auto.dashboard import DASHBOARD_HTML_PATH
    key = "?v=20260913-multi-cdp-v1"
    app = (ASSET_ROOT / "app.js").read_text(encoding="utf-8")
    html = DASHBOARD_HTML_PATH.read_text(encoding="utf-8")
    notify = (ASSET_ROOT / "views/notify.js").read_text(encoding="utf-8")
    for module in ("store.js", "views/board.js", "views/browsers.js", "views/task_detail.js", "views/notify.js"):
        assert f'"./{module}{key}"' in app
    assert f'"./task_detail.js{key}"' in notify
    assert f'/assets/app.js{key}' in html
    assert f'/assets/dashboard.css{key}' in html
