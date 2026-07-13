import os,signal,subprocess
from .config import BrowserConfig

def build_chromium_command(config:BrowserConfig,headless:bool):
    cmd=[config.chromium,f"--remote-debugging-address={config.cdp_host}",f"--remote-debugging-port={config.cdp_port}",f"--user-data-dir={config.profile_dir}","--no-first-run","--no-default-browser-check","--disable-dev-shm-usage","about:blank"]
    env={}
    if headless: cmd.insert(-1,"--headless=new")
    else: env["DISPLAY"]=config.display
    return cmd,env

def read_pid(config):
    try:
        pid=int(config.pid_file.read_text().strip()); os.kill(pid,0); return pid
    except (FileNotFoundError,ValueError,ProcessLookupError,PermissionError): return None

def start_browser(config,headless=False):
    if read_pid(config): raise RuntimeError("browser is already running")
    config.profile_dir.mkdir(parents=True,exist_ok=True)
    cmd,extra=build_chromium_command(config,headless)
    log=config.log_file.open("ab")
    p=subprocess.Popen(cmd,cwd=config.repo_root,env={**os.environ,**extra},stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
    config.pid_file.write_text(str(p.pid)); config.mode_file.write_text("headless" if headless else "gui")
    return p.pid

def stop_browser(config):
    pid=read_pid(config)
    if not pid: config.pid_file.unlink(missing_ok=True); return False
    os.killpg(pid,signal.SIGTERM); config.pid_file.unlink(missing_ok=True); config.mode_file.unlink(missing_ok=True); return True
