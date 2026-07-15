#!/usr/bin/env python3
"""Create a runtime Selkies package with bounded Tailscale-focused ICE."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def replace_once(source: str, old: str, new: str, label: str) -> str:
    if old not in source:
        raise RuntimeError(f"unsupported Selkies source: {label} anchor not found")
    return source.replace(old, new, 1)


def patch_gstwebrtc_app(source: str) -> str:
    marker = "playwright-auto: filter outbound ICE candidates"
    if marker in source:
        return source
    old = '''        logger.debug("received ICE candidate: %d %s", mlineindex, candidate)
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.on_ice(mlineindex, candidate))
'''
    new = '''        logger.debug("received ICE candidate: %d %s", mlineindex, candidate)
        # playwright-auto: filter outbound ICE candidates for the private viewer.
        parts = str(candidate or "").split()
        protocol = parts[2].lower() if len(parts) > 4 else ""
        address = parts[4] if len(parts) > 4 else ""
        udp_only = os.environ.get("SELKIES_ICE_UDP_ONLY", "false").lower() == "true"
        allowed = {
            value.strip()
            for value in os.environ.get("SELKIES_ALLOWED_ICE_ADDRESSES", "").split(",")
            if value.strip()
        }
        if udp_only and protocol != "udp":
            logger.debug("skipping non-UDP ICE candidate: %s", candidate)
            return
        if allowed and address not in allowed:
            logger.debug("skipping ICE address outside allowlist: %s", address)
            return
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.on_ice(mlineindex, candidate))
'''
    return replace_once(source, old, new, "ICE send")


def patch_main(source: str) -> str:
    marker = "playwright-auto: optional video-only backend"
    if marker in source:
        return source

    source = replace_once(
        source,
        '''    my_audio_id = 2
    audio_peer_id = 3
''',
        '''    my_audio_id = 2
    audio_peer_id = 3
    # playwright-auto: optional video-only backend.
    disable_audio = os.environ.get("SELKIES_DISABLE_AUDIO", "false").lower() == "true"
''',
        "audio flag",
    )
    source = replace_once(
        source,
        '''           # Waiting for peer to connect, retry in 2 seconds.
           time.sleep(2)
           await signalling.setup_call()
''',
        '''           # Non-blocking retry; blocking here stalls HTTP and WebSocket handling.
           await asyncio.sleep(float(os.environ.get("SELKIES_SIGNAL_RETRY_SECONDS", "0.25")))
           await signalling.setup_call()
''',
        "video signalling retry",
    )
    source = replace_once(
        source,
        '''           # Waiting for peer to connect, retry in 2 seconds.
           time.sleep(2)
           await audio_signalling.setup_call()
''',
        '''           # Non-blocking retry; audio may be disabled by the runtime wrapper.
           await asyncio.sleep(float(os.environ.get("SELKIES_SIGNAL_RETRY_SECONDS", "0.25")))
           await audio_signalling.setup_call()
''',
        "audio signalling retry",
    )
    source = replace_once(
        source,
        '''    audio_signalling.on_error = on_audio_signalling_error

    signalling.on_disconnect = lambda: app.stop_pipeline()
    audio_signalling.on_disconnect = lambda: audio_app.stop_pipeline()

    # After connecting, attempt to setup call to peer
    signalling.on_connect = signalling.setup_call
    audio_signalling.on_connect = audio_signalling.setup_call
''',
        '''    audio_signalling.on_error = on_audio_signalling_error

    signalling.on_disconnect = lambda: app.stop_pipeline()
    audio_signalling.on_disconnect = lambda: audio_app.stop_pipeline()

    # After connecting, attempt to setup call to peer.
    signalling.on_connect = signalling.setup_call
    if not disable_audio:
        audio_signalling.on_connect = audio_signalling.setup_call
''',
        "audio on-connect",
    )
    source = replace_once(
        source,
        '''        elif str(session_peer_id) == str(audio_peer_id):
            logger.info("starting audio pipeline")
            audio_app.start_pipeline(audio_only=True)
''',
        '''        elif str(session_peer_id) == str(audio_peer_id):
            if disable_audio:
                logger.info("audio pipeline disabled")
                return
            logger.info("starting audio pipeline")
            audio_app.start_pipeline(audio_only=True)
''',
        "audio session",
    )
    source = replace_once(
        source,
        '''            asyncio.ensure_future(app.handle_bus_calls(), loop=loop)
            asyncio.ensure_future(audio_app.handle_bus_calls(), loop=loop)

            loop.run_until_complete(signalling.connect())
            loop.run_until_complete(audio_signalling.connect())

            # asyncio.ensure_future(signalling.start(), loop=loop)
            asyncio.ensure_future(audio_signalling.start(), loop=loop)
            loop.run_until_complete(signalling.start())

            app.stop_pipeline()
            audio_app.stop_pipeline()
            webrtc_input.stop_js_server()
''',
        '''            asyncio.ensure_future(app.handle_bus_calls(), loop=loop)
            if not disable_audio:
                asyncio.ensure_future(audio_app.handle_bus_calls(), loop=loop)

            loop.run_until_complete(signalling.connect())
            if not disable_audio:
                loop.run_until_complete(audio_signalling.connect())

            # asyncio.ensure_future(signalling.start(), loop=loop)
            if not disable_audio:
                asyncio.ensure_future(audio_signalling.start(), loop=loop)
            loop.run_until_complete(signalling.start())

            app.stop_pipeline()
            if not disable_audio:
                audio_app.stop_pipeline()
            webrtc_input.stop_js_server()
''',
        "audio main loop",
    )
    source = replace_once(
        source,
        '''        app.stop_pipeline()
        audio_app.stop_pipeline()
        webrtc_input.stop_clipboard()
''',
        '''        app.stop_pipeline()
        if not disable_audio:
            audio_app.stop_pipeline()
        webrtc_input.stop_clipboard()
''',
        "audio cleanup",
    )
    return source


def prepare(source_package: Path, output_root: Path) -> Path:
    source_package = source_package.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    if not (source_package / "__main__.py").is_file():
        raise FileNotFoundError(source_package / "__main__.py")

    temporary = output_root.with_name(output_root.name + ".tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    temporary.mkdir(parents=True)
    target_package = temporary / "selkies_gstreamer"
    shutil.copytree(source_package, target_package)

    gst_path = target_package / "gstwebrtc_app.py"
    gst_path.write_text(patch_gstwebrtc_app(gst_path.read_text(encoding="utf-8")), encoding="utf-8")
    main_path = target_package / "__main__.py"
    main_path.write_text(patch_main(main_path.read_text(encoding="utf-8")), encoding="utf-8")

    shutil.rmtree(output_root, ignore_errors=True)
    temporary.replace(output_root)
    return output_root


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(prepare(args.source, args.output))


if __name__ == "__main__":
    main()
