#!/usr/bin/env bash
# Benchmark the deployed Modal voice endpoint: cold-start + warm /tts + /stt.
# Usage: deploy/modal/bench.sh <base-url> [tts-token]
set -uo pipefail
BASE="${1:?usage: bench.sh <base-url> [token]}"
TOK="${2:-}"
AUTH=(); [ -n "$TOK" ] && AUTH=(-H "Authorization: Bearer $TOK")
hr(){ printf '%s\n' "----------------------------------------"; }

echo "endpoint: $BASE"; hr
echo "[1] COLD /health (first hit wakes a container)"
curl -s -o /dev/null -w "  health: http=%{http_code}  total=%{time_total}s\n" --max-time 90 "$BASE/health"

echo "[2] COLD /tts (model already loaded if warm; else full cold start)"
curl -s -o /tmp/bench_tts1.wav -w "  tts#1 : http=%{http_code}  bytes=%{size_download}  total=%{time_total}s\n" \
  --max-time 120 "${AUTH[@]}" -H 'content-type: application/json' \
  -d '{"text":"Hey, this is the lfg voice running on Modal."}' "$BASE/tts"

echo "[3] WARM /tts x3 (steady-state synth latency)"
for i in 1 2 3; do
  curl -s -o /tmp/bench_tts_w.wav -w "  tts warm#$i: http=%{http_code}  bytes=%{size_download}  total=%{time_total}s\n" \
    --max-time 60 "${AUTH[@]}" -H 'content-type: application/json' \
    -d '{"text":"Everything is idle. Nine sessions are sitting quietly."}' "$BASE/tts"
done

echo "[4] /stt round-trip (uses the WAV we just synthesized)"
if [ -s /tmp/bench_tts1.wav ]; then
  curl -s -w "\n  stt: http=%{http_code}  total=%{time_total}s\n" \
    --max-time 60 "${AUTH[@]}" --data-binary @/tmp/bench_tts1.wav "$BASE/stt"
fi
hr; echo "done. tts audio saved at /tmp/bench_tts1.wav (play to sanity-check voice)."
