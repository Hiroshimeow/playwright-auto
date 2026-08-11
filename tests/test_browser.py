import os
import stat
import subprocess
from pathlib import Path

import pytest
from playwright.sync_api import Error, sync_playwright

from playwright_auto.browser import build_chromium_command
from playwright_auto.config import BrowserConfig, resolve_chromium


def test_pytest_playwright_tmp_root_is_repo_owned():
    repo_root = Path(__file__).resolve().parents[1]
    expected = (repo_root / "test-results" / "playwright-tmp").resolve()
    actual = Path(os.environ.get("TMPDIR", ".")).resolve()

    assert actual == expected
    assert stat.S_IMODE(actual.stat().st_mode) == 0o700
    assert not actual.is_relative_to(Path("/tmp"))
    assert actual != Path("/home/ayumi/Workspace/playwright-profile")


def _profile_paths(root: Path) -> set[Path]:
    return {path.resolve() for path in root.glob("playwright_chromiumdev_profile-*") if path.is_dir()}


def _snap_profile_names() -> set[str]:
    result = subprocess.run(
        [
            "snap",
            "run",
            "--shell",
            "chromium",
            "-c",
            "find /tmp -maxdepth 1 -type d -name 'playwright_chromiumdev_profile-*' -printf '%f\\n'",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return set(result.stdout.splitlines())


def _assert_no_new_profiles(
    playwright_tmpdir: Path,
    before_repo: set[Path],
    before_snap: set[str],
) -> None:
    assert _profile_paths(playwright_tmpdir) == before_repo
    assert _snap_profile_names() == before_snap


def test_disposable_snap_chromium_profiles_are_removed(playwright_tmpdir):
    snap_chromium = Path("/snap/bin/chromium")
    if not snap_chromium.exists():
        pytest.skip("Snap Chromium is unavailable")

    before_repo = _profile_paths(playwright_tmpdir)
    before_snap = _snap_profile_names()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(snap_chromium), headless=True)
        assert len(_profile_paths(playwright_tmpdir) - before_repo) == 1
        browser.close()
    _assert_no_new_profiles(playwright_tmpdir, before_repo, before_snap)

    before_repo = _profile_paths(playwright_tmpdir)
    before_snap = _snap_profile_names()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=str(snap_chromium), headless=True)
        try:
            raise RuntimeError("deliberate test-body failure")
        except RuntimeError:
            pass
        finally:
            browser.close()
    _assert_no_new_profiles(playwright_tmpdir, before_repo, before_snap)

    before_repo = _profile_paths(playwright_tmpdir)
    before_snap = _snap_profile_names()
    with sync_playwright() as playwright:
        with pytest.raises(Error):
            playwright.chromium.launch(executable_path="/bin/false", headless=True)
    _assert_no_new_profiles(playwright_tmpdir, before_repo, before_snap)


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
