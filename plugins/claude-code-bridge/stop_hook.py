"""Claude Code Stop hook for claude-code-bridge.

Copied into each channel directory and run from there by Claude Code. It reads the
hook JSON from stdin and writes ``outbox/<time_ns>.json`` next to itself. It never
posts to chat, never prints, and always exits 0 so it cannot block the turn.
"""

import json
import os
import sys
import time
from pathlib import Path


def main() -> None:
    raw = sys.stdin.read()
    data = json.loads(raw)
    if not isinstance(data, dict):
        return
    outbox = Path(__file__).resolve().parent / "outbox"
    outbox.mkdir(parents=True, exist_ok=True)
    text = data.get("last_assistant_message") or ""
    payload = {
        "session_id": data.get("session_id"),
        "text": text if isinstance(text, str) else str(text),
        "ts": time.time(),
    }
    stamp = time.time_ns()
    while (outbox / f"{stamp}.json").exists():
        stamp += 1
    final = outbox / f"{stamp}.json"
    tmp = outbox / f".{final.name}.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
