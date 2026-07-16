from __future__ import annotations

from pathlib import Path

from playwright_auto.studio.app import build_parser
from playwright_auto.studio.smoke import build_parser as build_smoke_parser


def test_studio_parser_defaults_to_local_cdp_and_runtime_dir():
    args = build_parser().parse_args([])

    assert args.cdp == "http://127.0.0.1:9222"
    assert args.runtime_dir == ".runtime/studio"
    assert args.geometry == "1480x900"
    assert args.no_auto_connect is False


def test_studio_smoke_parser_is_read_only_by_default():
    args = build_smoke_parser().parse_args([])

    assert args.cdp == "http://127.0.0.1:9222"
    assert args.pretty is False


def test_pyproject_exports_studio_console_script():
    root = Path(__file__).resolve().parents[1]
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")

    assert 'playwright-studio = "playwright_auto.studio.app:main"' in pyproject
    assert 'playwright-studio-smoke = "playwright_auto.studio.smoke:main"' in pyproject
