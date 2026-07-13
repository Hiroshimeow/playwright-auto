from pathlib import Path
from playwright_auto.browser import build_chromium_command
from playwright_auto.config import BrowserConfig

def test_defaults_are_local_and_repo_scoped():
    c=BrowserConfig.from_repo(Path("/repo"))
    assert (c.cdp_host,c.cdp_port,c.viewer_port,c.display)==("127.0.0.1",9222,9223,":100")
    assert c.profile_dir==Path("/repo/.runtime/main-profile")

def test_gui_and_headless_commands():
    c=BrowserConfig.from_repo(Path("/repo"),chromium="/usr/bin/chromium")
    gui,env=build_chromium_command(c,False)
    assert "--remote-debugging-port=9222" in gui and "--user-data-dir=/repo/.runtime/main-profile" in gui
    assert env["DISPLAY"]==":100" and "--headless=new" not in gui
    headless,env=build_chromium_command(c,True)
    assert "--headless=new" in headless and "DISPLAY" not in env
