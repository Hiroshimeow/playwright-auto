from pathlib import Path
from playwright_auto.config import BrowserConfig

def test_streaming_is_independent_from_cdp():
    c=BrowserConfig.from_repo(Path("/repo"))
    assert c.cdp_url=="http://127.0.0.1:9222"
    assert (c.viewer_host,c.viewer_port,c.viewer_url)==("0.0.0.0",9223,"http://127.0.0.1:9223/")
