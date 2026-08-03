from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pm2_uses_vnc_and_novnc_viewer_only():
    ecosystem = (ROOT / "ecosystem.config.cjs").read_text(encoding="utf-8")
    assert "playwright-display" in ecosystem
    assert "playwright-vnc" in ecosystem
    assert "playwright-novnc" in ecosystem
    assert ("sel" + "kies") not in ecosystem.lower()


def test_vnc_backend_is_loopback_and_shared():
    script = (ROOT / "scripts" / "vnc-start.sh").read_text(encoding="utf-8")
    assert "-localhost" in script
    assert "-forever" in script
    assert "-shared" in script
    assert "PLAYWRIGHT_DISPLAY" in script
    assert "PLAYWRIGHT_VNC_PORT" in script


def test_novnc_proxy_keeps_vnc_and_websocket_backends_on_loopback():
    script = (ROOT / "scripts" / "novnc-start.sh").read_text(encoding="utf-8")
    assert 'proxy_pass http://127.0.0.1:$WEBSOCKET_PORT' in script
    assert '"127.0.0.1:$WEBSOCKET_PORT" "127.0.0.1:$VNC_PORT"' in script
    assert 'listen 0.0.0.0:$WEB_PORT' in script
    assert '"view_only": false' in script


def test_novnc_install_is_version_pinned_and_repo_local():
    script = (ROOT / "scripts" / "install-novnc.sh").read_text(encoding="utf-8")
    assert 'NOVNC_VERSION="${NOVNC_VERSION:-1.7.0}"' in script
    assert 'WEBSOCKIFY_VERSION="${WEBSOCKIFY_VERSION:-0.13.0}"' in script
    assert 'RUNTIME_ROOT="$REPO_ROOT/.runtime"' in script


def test_repository_has_no_retired_viewer_runtime_or_documentation_references():
    excluded = {".git", ".plan", ".runtime", ".venv", ".pytest_cache", "__pycache__"}
    matches = []
    for path in ROOT.rglob("*"):
        if (
            not path.is_file()
            or path == Path(__file__).resolve()
            or any(part in excluded for part in path.parts)
        ):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if ("sel" + "kies") in text.lower():
            matches.append(str(path.relative_to(ROOT)))
    assert matches == []
