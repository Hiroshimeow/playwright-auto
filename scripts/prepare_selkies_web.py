#!/usr/bin/env python3
"""Create a repo-controlled Selkies web root optimized for video-only viewing."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

AUDIO_CONSTRUCTORS = '''var audio_signalling = new WebRTCDemoSignalling(new URL(protocol + window.location.host + "/" + app.appName + "/signalling/"));
var audio_webrtc = new WebRTCDemo(audio_signalling, audioElement, 3);'''

AUDIO_STUB = '''// playwright-auto: video-only viewer. Avoid a second WebRTC/audio signalling session.
var audio_signalling = {
    disconnect() {},
    onstatus: null,
    onerror: null,
    ondisconnect: null,
    ondebug: null,
};
var audio_webrtc = {
    forceTurn: false,
    rtcPeerConfig: null,
    peerConnection: { getReceivers: () => [] },
    playStream() {},
    reset() {},
    getConnectionStats() {
        return Promise.resolve({
            general: {
                currentRoundTripTime: null,
                connectionType: "disabled",
                bytesReceived: 0,
                bytesSent: 0,
                availableReceiveBandwidth: 0,
            },
            audio: {
                packetsReceived: 0,
                packetsLost: 0,
                codecName: "disabled",
                bytesReceived: 0,
                jitterBufferDelay: 0,
                jitterBufferEmittedCount: 0,
            },
            allReports: {},
        });
    },
    connect() {
        queueMicrotask(() => this.onconnectionstatechange?.("connected"));
    },
    onstatus: null,
    onerror: null,
    ondebug: null,
    onconnectionstatechange: null,
    onplaystreamrequired: null,
};'''


SERVICE_WORKER_HANDLER = "navigator.serviceWorker.addEventListener('message', event => {"
SERVICE_WORKER_GUARD = "navigator.serviceWorker?.addEventListener('message', event => {"


def patch_index(source: str) -> str:
    if SERVICE_WORKER_GUARD in source:
        return source
    if SERVICE_WORKER_HANDLER not in source:
        raise RuntimeError(
            "unsupported Selkies index.html: service worker anchor not found"
        )
    return source.replace(SERVICE_WORKER_HANDLER, SERVICE_WORKER_GUARD, 1)


def patch_app(source: str) -> str:
    if "playwright-auto: video-only viewer" in source:
        return source
    if AUDIO_CONSTRUCTORS not in source:
        raise RuntimeError("unsupported Selkies app.js: audio constructor anchor not found")
    patched = source.replace(AUDIO_CONSTRUCTORS, AUDIO_STUB, 1)
    patched = patched.replace('var audioConnected = "";', 'var audioConnected = "connected";', 1)
    if patched == source:
        raise RuntimeError("Selkies app.js patch produced no change")
    return patched


def prepare(source_root: Path, output_root: Path) -> Path:
    source_root = source_root.expanduser().resolve()
    output_root = output_root.expanduser().resolve()
    source_app = source_root / "app.js"
    source_index = source_root / "index.html"
    if not source_app.is_file():
        raise FileNotFoundError(source_app)
    if not source_index.is_file():
        raise FileNotFoundError(source_index)

    temporary = output_root.with_name(output_root.name + ".tmp")
    shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(source_root, temporary)
    target_app = temporary / "app.js"
    target_app.write_text(
        patch_app(target_app.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
    target_index = temporary / "index.html"
    target_index.write_text(
        patch_index(target_index.read_text(encoding="utf-8")),
        encoding="utf-8",
    )
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
