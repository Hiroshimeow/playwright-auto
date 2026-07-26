from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vnc_backend_is_private_shared_and_targets_browser_display():
    script = (ROOT / "scripts" / "vnc-start.sh").read_text(encoding="utf-8")
    assert 'PLAYWRIGHT_DISPLAY:-:100' in script
    assert 'PLAYWRIGHT_VNC_PORT:-5901' in script
    assert "-localhost" in script
    assert "-forever" in script
    assert "-shared" in script
    assert "-nopw" in script


def test_novnc_frontend_rejects_legacy_selkies_paths():
    script = (ROOT / "scripts" / "novnc-start.sh").read_text(encoding="utf-8")
    assert 'PLAYWRIGHT_VIEWER_PORT:-9223' in script
    assert 'PLAYWRIGHT_WEBSOCKET_PORT:-9226' in script
    assert 'location = /websockify' in script
    assert 'location ^~ /webrtc/' in script
    assert "return 410" in script
    assert '"autoconnect": true' in script
    assert '"reconnect": true' in script
    assert '"resize": "scale"' in script
    assert '"shared": true' in script


def test_pm2_uses_independent_vnc_and_novnc_services():
    ecosystem = (ROOT / "ecosystem.config.cjs").read_text(encoding="utf-8")
    assert 'name: "playwright-display"' in ecosystem
    assert 'name: "playwright-vnc"' in ecosystem
    assert 'name: "playwright-novnc"' in ecosystem
    assert 'name: "playwright-browser"' in ecosystem
    assert "playwright-selkies" not in ecosystem


def test_installer_pins_reproducible_runtime_versions():
    script = (ROOT / "scripts" / "install-novnc.sh").read_text(encoding="utf-8")
    assert 'NOVNC_VERSION="${NOVNC_VERSION:-1.7.0}"' in script
    assert 'WEBSOCKIFY_VERSION="${WEBSOCKIFY_VERSION:-0.13.0}"' in script
    assert "github.com/novnc/noVNC/archive/refs/tags" in script
    assert '"websockify==$WEBSOCKIFY_VERSION"' in script
