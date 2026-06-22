#!/usr/bin/env bash
set -a; [ -f /opt/stt/stt.env ] && . /opt/stt/stt.env; set +a
export PATH=/opt/conda/bin:$PATH
exec python /opt/stt/whisper_stt.py
