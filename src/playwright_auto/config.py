from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


def resolve_chromium(explicit: str | None = None) -> str:
    """Resolve a Chromium-family executable without assuming Snap packaging."""
    if explicit:
        return explicit
    configured = os.environ.get("CHROMIUM_BIN", "").strip()
    if configured:
        return configured
    for command in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
    ):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    snap_chromium = Path("/snap/bin/chromium")
    if snap_chromium.is_file():
        return str(snap_chromium)
    return "chromium"


@dataclass(frozen=True)
class BrowserConfig:
    repo_root: Path
    chromium: str
    cdp_host: str = "127.0.0.1"
    cdp_port: int = 9222
    viewer_host: str = "0.0.0.0"
    viewer_port: int = 9223
    display: str = ":100"

    @classmethod
    def from_repo(
        cls,
        repo_root: Path,
        chromium: str | None = None,
    ) -> "BrowserConfig":
        return cls(repo_root.resolve(), resolve_chromium(chromium))

    @property
    def runtime_dir(self) -> Path:
        return self.repo_root / ".runtime"

    @property
    def profile_dir(self) -> Path:
        return self.runtime_dir / "main-profile"

    @property
    def pid_file(self) -> Path:
        return self.runtime_dir / "browser.pid"

    @property
    def mode_file(self) -> Path:
        return self.runtime_dir / "browser.mode"

    @property
    def log_file(self) -> Path:
        return self.runtime_dir / "browser.log"

    @property
    def cdp_url(self) -> str:
        return f"http://{self.cdp_host}:{self.cdp_port}"

    @property
    def viewer_url(self) -> str:
        return f"http://127.0.0.1:{self.viewer_port}/"
