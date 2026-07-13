import argparse
from pathlib import Path
from .browser import read_pid,start_browser,stop_browser
from .config import BrowserConfig

def main():
    p=argparse.ArgumentParser(); p.add_argument("--repo",type=Path,default=Path.cwd())
    sub=p.add_subparsers(dest="command",required=True)
    for name in ("start","restart"):
        x=sub.add_parser(name); x.add_argument("--headless",action="store_true")
    sub.add_parser("stop"); sub.add_parser("status")
    a=p.parse_args(); c=BrowserConfig.from_repo(a.repo)
    if a.command=="start": print(start_browser(c,a.headless))
    elif a.command=="stop": print("stopped" if stop_browser(c) else "not running")
    elif a.command=="restart": stop_browser(c); print(start_browser(c,a.headless))
    else:
        pid=read_pid(c); print(f"running pid={pid}" if pid else "stopped"); return 0 if pid else 1
    return 0
