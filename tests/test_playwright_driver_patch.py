from pathlib import Path

import pytest

from playwright_auto.playwright_driver_patch import patch_dialog_autoclose_rejection


UNSAFE = b"""if (!hasHandlers)
          dialog._close().then(() => {
          });"""
SAFE = b"""if (!hasHandlers)
          dialog._close().catch(() => {
          });"""


def test_patch_dialog_autoclose_rejection_guards_implicit_close(tmp_path: Path) -> None:
    bundle = tmp_path / "coreBundle.js"
    bundle.write_bytes(b"before\n" + UNSAFE + b"\nafter\n")

    changed = patch_dialog_autoclose_rejection(bundle)

    assert changed is True
    assert UNSAFE not in bundle.read_bytes()
    assert SAFE in bundle.read_bytes()
    assert patch_dialog_autoclose_rejection(bundle) is False


def test_patch_dialog_autoclose_rejection_fails_closed_on_mixed_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "coreBundle.js"
    original = SAFE + b"\n" + UNSAFE
    bundle.write_bytes(original)

    with pytest.raises(RuntimeError, match="dialog auto-close guard"):
        patch_dialog_autoclose_rejection(bundle)

    assert bundle.read_bytes() == original


def test_patch_dialog_autoclose_rejection_fails_closed_on_unknown_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "coreBundle.js"
    bundle.write_text("unexpected upstream shape", encoding="utf-8")

    with pytest.raises(RuntimeError, match="dialog auto-close guard"):
        patch_dialog_autoclose_rejection(bundle)
