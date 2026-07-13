from pathlib import Path

def test_selkies_stream_is_mobile_resizable_and_unauthenticated():
    script=Path("scripts/selkies-start.sh").read_text()
    assert "--port=9223" in script
    assert "--enable_resize=true" in script
    assert "--enable_basic_auth=false" in script
    assert "--encoder=x264enc" in script

def test_pm2_uses_selkies_not_kasmvnc():
    ecosystem=Path("ecosystem.config.cjs").read_text()
    assert "playwright-display" in ecosystem
    assert "playwright-selkies" in ecosystem
    assert "playwright-kasmvnc" not in ecosystem
