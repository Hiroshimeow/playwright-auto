from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class BrowserConfig:
    repo_root: Path
    chromium: str="/snap/bin/chromium"
    cdp_host: str="127.0.0.1"
    cdp_port: int=9222
    viewer_host: str="0.0.0.0"
    viewer_port: int=9223
    display: str=":100"

    @classmethod
    def from_repo(cls,repo_root:Path,chromium:str="/snap/bin/chromium"):
        return cls(repo_root.resolve(),chromium)

    @property
    def runtime_dir(self): return self.repo_root/".runtime"
    @property
    def profile_dir(self): return self.runtime_dir/"main-profile"
    @property
    def pid_file(self): return self.runtime_dir/"browser.pid"
    @property
    def mode_file(self): return self.runtime_dir/"browser.mode"
    @property
    def log_file(self): return self.runtime_dir/"browser.log"
    @property
    def cdp_url(self): return f"http://{self.cdp_host}:{self.cdp_port}"
    @property
    def viewer_url(self): return f"http://127.0.0.1:{self.viewer_port}/"
