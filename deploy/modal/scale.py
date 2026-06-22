#!/usr/bin/env python3
# Power the lfg-voice Modal GPU up/down by toggling the Voice class autoscaler.
#   on  -> min_containers=1  (keep one L4 warm: snappy, ~$0.80/hr)
#   off -> min_containers=0  (scale to zero: ~$0 idle, cold start on next use)
# Run from the dev box (uses ~/.modal.toml auth):
#   uv run --with modal python deploy/modal/scale.py on|off|status
import sys

import modal

arg = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
voice = modal.Cls.from_name("lfg-voice", "Voice")
inst = voice()  # autoscaler is set on the class instance

if arg in ("on", "1", "true", "up"):
    inst.update_autoscaler(min_containers=1)
    print("ok on (min_containers=1, warm)")
elif arg in ("off", "0", "false", "down"):
    inst.update_autoscaler(min_containers=0)
    print("ok off (min_containers=0, scale-to-zero)")
else:
    print("usage: scale.py on|off")
    sys.exit(2)
