import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name: str):
    path = ROOT / "scripts" / name
    spec = importlib.util.spec_from_file_location(name.replace(".", "_"), path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_web_patch_disables_audio_and_guards_http_service_worker():
    module = load_script("prepare_selkies_web.py")
    app = (
        'var audioConnected = "";\n'
        + module.AUDIO_CONSTRUCTORS
        + "\nconst untouched = true;\n"
    )
    patched_app = module.patch_app(app)
    assert "playwright-auto: video-only viewer" in patched_app
    assert module.AUDIO_CONSTRUCTORS not in patched_app
    assert 'var audioConnected = "connected";' in patched_app

    index = module.SERVICE_WORKER_HANDLER + "\n"
    patched_index = module.patch_index(index)
    assert module.SERVICE_WORKER_GUARD in patched_index
    assert module.patch_index(patched_index) == patched_index


def test_python_patch_filters_ice_candidates():
    module = load_script("prepare_selkies_python.py")
    source = '''        logger.debug("received ICE candidate: %d %s", mlineindex, candidate)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.on_ice(mlineindex, candidate))
'''
    patched = module.patch_gstwebrtc_app(source)
    assert "SELKIES_ICE_UDP_ONLY" in patched
    assert "SELKIES_ALLOWED_ICE_ADDRESSES" in patched
    assert "skipping non-UDP ICE candidate" in patched


def test_selkies_start_discovers_python_package_without_version_pin():
    startup = (ROOT / "scripts" / "selkies-start.sh").read_text(encoding="utf-8")
    assert "python3.12/site-packages" not in startup
    assert "SELKIES_PYTHON_PACKAGE" in startup
    assert 'find "$SELKIES_ROOT/lib"' in startup


def test_python_runtime_patch_matches_installed_selkies_when_available():
    module = load_script("prepare_selkies_python.py")
    packages = sorted(
        (Path.home() / ".local/opt/selkies-gstreamer/lib").glob(
            "python*/site-packages/selkies_gstreamer"
        )
    )
    if not packages:
        pytest.skip("local Selkies package is not installed")
    package = packages[-1]
    main_path = package / "__main__.py"
    gst_path = package / "gstwebrtc_app.py"
    if not main_path.is_file() or not gst_path.is_file():
        pytest.skip("local Selkies package is not installed")

    patched_main = module.patch_main(main_path.read_text(encoding="utf-8"))
    patched_gst = module.patch_gstwebrtc_app(gst_path.read_text(encoding="utf-8"))
    assert "SELKIES_DISABLE_AUDIO" in patched_main
    assert "SELKIES_SIGNAL_RETRY_SECONDS" in patched_main
    assert "await asyncio.sleep" in patched_main
    assert "SELKIES_ALLOWED_ICE_ADDRESSES" in patched_gst
