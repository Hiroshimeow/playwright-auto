#!/usr/bin/env bash
set -euo pipefail
rm -f /tmp/.X100-lock /tmp/.X11-unix/X100
exec /usr/bin/Xvfb :100 -screen 0 1920x1080x24 +extension COMPOSITE +extension DAMAGE +extension GLX +extension RANDR +extension RENDER +extension MIT-SHM +extension XFIXES +extension XTEST +iglx +render -nolisten tcp -ac -noreset
