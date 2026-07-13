# playwright-auto

Persistent Chromium automation through local CDP, with a live interactive KasmVNC view.

## Endpoints

- CDP: `http://127.0.0.1:9222` (loopback only)
- Viewer: `http://<tailscale-ip>:9223/` (interactive browser screen)
- Profile: `.runtime/main-profile/` (inside this repository, ignored by Git)
- Display: `:100`

Port 9223 is only the visual keyboard/mouse view. Automation connects only to CDP 9222.

## Start

```bash
uv sync
pm2 start ecosystem.config.cjs
pm2 save
```

```bash
pm2 status playwright-kasmvnc playwright-browser
curl http://127.0.0.1:9222/json/version
curl -I http://127.0.0.1:9223/
uv run python scripts/smoke.py
```

Stop or restart with `pm2 stop|restart playwright-browser playwright-kasmvnc`.

KasmVNC is unpacked user-locally at `~/.local/opt/kasmvnc`. The viewer intentionally has no KasmVNC password and must stay inside the private Tailscale network. Do not expose port 9223 through Funnel or a public tunnel.

The stream is isolated behind `scripts/kasmvnc-start.sh`, so Selkies/WebRTC can later replace it without changing Chrome, profile, CDP, or Playwright clients.

## Headless

Stop both PM2 apps before reusing the profile headlessly:

```bash
pm2 stop playwright-browser playwright-kasmvnc
uv run playwright-auto start --headless
uv run playwright-auto status
uv run playwright-auto stop
```

GUI and headless must never run simultaneously with the same profile.
