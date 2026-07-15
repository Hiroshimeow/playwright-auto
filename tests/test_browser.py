from pathlib import Path

from playwright_auto.browser import build_chromium_command
from playwright_auto.config import BrowserConfig, resolve_chromium

def test_defaults_are_local_and_repo_scoped(tmp_path):
    c = BrowserConfig.from_repo(tmp_path)
    assert (c.cdp_host, c.cdp_port, c.viewer_port, c.display) == (
        "127.0.0.1",
        9222,
        9223,
        ":100",
    )
    assert c.profile_dir == tmp_path.resolve() / ".runtime" / "main-profile"


def test_gui_and_headless_commands(tmp_path):
    c = BrowserConfig.from_repo(tmp_path, chromium="/usr/bin/chromium")
    gui, env = build_chromium_command(c, False)
    assert "--remote-debugging-port=9222" in gui
    assert f"--user-data-dir={c.profile_dir}" in gui
    assert env["DISPLAY"] == ":100" and "--headless=new" not in gui
    headless, env = build_chromium_command(c, True)
    assert "--headless=new" in headless and "DISPLAY" not in env


def test_chromium_resolution_prefers_explicit_then_environment(monkeypatch):
    monkeypatch.setenv("CHROMIUM_BIN", "/custom/chrome")
    assert resolve_chromium("/explicit/chrome") == "/explicit/chrome"
    assert resolve_chromium() == "/custom/chrome"


def test_runtime_scripts_do_not_require_snap_or_fixed_xvfb_paths():
    root = Path(__file__).resolve().parents[1]
    browser_script = (root / "scripts/browser-gui.sh").read_text(encoding="utf-8")
    xvfb_script = (root / "scripts/xvfb-start.sh").read_text(encoding="utf-8")
    assert "CHROMIUM_BIN" in browser_script
    assert "command -v" in browser_script
    assert "exec /snap/bin/chromium" not in browser_script
    assert "XVFB_BIN" in xvfb_script
    assert "exec /usr/bin/Xvfb" not in xvfb_script
