#!/usr/bin/env python3
# Power the lfg-voice Modal GPU up/down by toggling the Voice class autoscaler.
#   on  -> min_containers=1  (keep one L4 warm: snappy, ~$0.80/hr)
#   off -> min_containers=0  (scale to zero: ~$0 idle, cold start on next use)
# Run from the dev box (uses ~/.modal.toml auth):
#   uv run --with modal python deploy/modal/scale.py on|off|status
import sys
from pathlib import Path

import modal

# Records the last-set power so the UI can reflect it WITHOUT pinging the GPU
# (a /health hit would cold-start a scaled-to-zero container — a cost footgun).
STATE = Path.home() / ".local" / "state" / "lfg-voice-power"
arg = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()

if arg in ("status", "state"):
    print(STATE.read_text().strip() if STATE.exists() else "off")
    sys.exit(0)

voice = modal.Cls.from_name("lfg-voice", "Voice")
inst = voice()  # autoscaler is set on the class instance
STATE.parent.mkdir(parents=True, exist_ok=True)

if arg in ("on", "1", "true", "up"):
    inst.update_autoscaler(min_containers=1)
    STATE.write_text("on")
    print("ok on (min_containers=1, warm)")
elif arg in ("off", "0", "false", "down"):
    inst.update_autoscaler(min_containers=0)
    STATE.write_text("off")
    print("ok off (min_containers=0, scale-to-zero)")
else:
    print("usage: scale.py on|off|status")
    sys.exit(2)
