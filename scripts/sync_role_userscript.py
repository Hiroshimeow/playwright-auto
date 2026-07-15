from __future__ import annotations

from pathlib import Path

from playwright_auto.role_indicator import ROLE_INDICATOR_SCRIPT

PATH = Path(__file__).resolve().parents[1] / "examples" / "chatgpt-playwright-adapter.user.js"
MARKER = "// PLAYWRIGHT_AUTO_ROLE_CONTROL_END\n"


def main() -> int:
    text = PATH.read_text(encoding="utf-8")
    body_start = text.find("(() => {")
    if body_start < 0:
        raise RuntimeError("userscript body not found")
    metadata = text[:body_start]
    old_body = text[body_start:]
    if MARKER in text:
        old_body = text.split(MARKER, 1)[1]
    metadata = metadata.replace("// @version      0.2.0", "// @version      0.3.0")
    PATH.write_text(
        metadata + ROLE_INDICATOR_SCRIPT.rstrip() + "\n" + MARKER + old_body,
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
