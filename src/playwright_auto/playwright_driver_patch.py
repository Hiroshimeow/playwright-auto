from __future__ import annotations

import importlib.util
import os
from pathlib import Path


_UNSAFE = b"""if (!hasHandlers)
          dialog._close().then(() => {
          });"""
_SAFE = b"""if (!hasHandlers)
          dialog._close().catch(() => {
          });"""


def playwright_core_bundle() -> Path:
    spec = importlib.util.find_spec("playwright")
    if spec is None or spec.origin is None:
        raise RuntimeError("Playwright package is not installed")
    return Path(spec.origin).resolve().parent / "driver" / "package" / "lib" / "coreBundle.js"


def patch_dialog_autoclose_rejection(bundle: Path) -> bool:
    data = bundle.read_bytes()
    unsafe_count = data.count(_UNSAFE)
    safe_count = data.count(_SAFE)
    if unsafe_count == 0 and safe_count == 1:
        return False
    if unsafe_count != 1 or safe_count != 0:
        raise RuntimeError(
            "Playwright dialog auto-close guard does not match the verified bundled driver shape"
        )
    patched = data.replace(_UNSAFE, _SAFE, 1)
    temporary = bundle.with_name(f"{bundle.name}.cdpa-{os.getpid()}.tmp")
    temporary.write_bytes(patched)
    os.replace(temporary, bundle)
    return True


def ensure_playwright_dialog_autoclose_guard() -> bool:
    return patch_dialog_autoclose_rejection(playwright_core_bundle())
