#!/usr/bin/env bash
# cp-sql.sh — read-only control-plane SQLite bridge for lfg auto agents.
#
# Convex is gone (vibes commit 43d3026); the omg.dev dashboard DB is now a plain
# SQLite file inside a docker volume on the omg-controlplane box. There is no
# bulk-read HTTP endpoint (generic auto-CRUD is deliberately off — every read
# goes through per-user scoped functions), so fleet-wide reads go straight at
# the file, read-only, over SSH. `sqlite3` isn't installed on the box but
# `python3` is; its stdlib opens the file with `mode=ro&immutable=1` — a
# lock-free read that never interferes with the live writer or litestream.
#
# Mirrors src/agents/collectors/controlplane.ts (collectControlplaneSql) so the
# script path the auto-agent prompts reference actually exists and behaves the
# same. Schema source of truth: vibes control-plane/schema.ts.
#
# Usage:
#   scripts/cp-sql.sh "SELECT id, title FROM bugReports WHERE status='open' ORDER BY createdAt DESC"
#   scripts/cp-sql.sh "<SQL>" [rowLimit]      # rowLimit default 200, max 1000
#
# Output: tab-separated, header row first; wide cells truncated to 300 chars.
# Only a SINGLE read-only SELECT/WITH statement is accepted — anything else is
# refused before it ever reaches the box.
set -euo pipefail

CP_SSH="${TWCLI_CP_SSH:-root@178.105.154.227}"
CP_DB="${TWCLI_CP_DB:-/var/lib/docker/volumes/omg-controlplane_cp_data/_data/controlplane.db}"
SSH_TIMEOUT_S=30

SQL="${1:-}"
LIMIT="${2:-200}"

if [[ -z "${SQL//[[:space:]]/}" ]]; then
  echo "usage: cp-sql.sh \"<SELECT ...>\" [rowLimit]" >&2
  exit 2
fi

# Clamp the row limit to [1, 1000].
if ! [[ "$LIMIT" =~ ^[0-9]+$ ]]; then LIMIT=200; fi
if (( LIMIT < 1 )); then LIMIT=1; fi
if (( LIMIT > 1000 )); then LIMIT=1000; fi

# Read-only guard (belt-and-suspenders on top of the mode=ro open): strip a
# trailing semicolon, reject multiple statements, require a leading SELECT/WITH,
# and forbid any write/DDL keyword.
guard="${SQL%"${SQL##*[![:space:]]}"}"   # rtrim
guard="${guard%;}"                        # drop one trailing ;
guard="${guard#"${guard%%[![:space:]]*}"}" # ltrim
if [[ "$guard" == *";"* ]]; then
  echo "ERROR: only a single statement is allowed (no ';')" >&2
  exit 2
fi
shopt -s nocasematch
if [[ ! "$guard" =~ ^(select|with)[[:space:]] ]]; then
  echo "ERROR: only read-only SELECT/WITH queries are allowed" >&2
  exit 2
fi
if [[ "$guard" =~ \b(insert|update|delete|drop|alter|create|replace|attach|detach|vacuum|reindex|pragma)\b ]]; then
  echo "ERROR: write/DDL keyword rejected — read-only SELECT/WITH only" >&2
  exit 2
fi
shopt -u nocasematch

# Pass SQL as base64 to dodge all shell/heredoc quoting. The python prog reads
# it from a literal, opens the DB read-only, truncates wide cells so one
# plannerState/runMessages.text blob can't blow the output, and caps rows.
B64="$(printf '%s' "$SQL" | base64 | tr -d '\n')"

ssh -o BatchMode=yes -o ConnectTimeout=10 "$CP_SSH" \
  "timeout ${SSH_TIMEOUT_S} python3 - <<'PYEOF'
import sqlite3, base64
SQL = base64.b64decode(\"${B64}\").decode()
DB = \"${CP_DB}\"
LIMIT = ${LIMIT}
def cell(v):
    if v is None: return \"\"
    s = str(v)
    return s if len(s) <= 300 else s[:300] + \"…\"
try:
    c = sqlite3.connect(f\"file:{DB}?mode=ro&immutable=1\", uri=True)
    cur = c.execute(SQL)
    cols = [d[0] for d in cur.description] if cur.description else []
    if cols: print(\"\\t\".join(cols))
    rows = cur.fetchmany(LIMIT)
    for r in rows:
        print(\"\\t\".join(cell(v) for v in r))
    if cur.fetchone() is not None:
        print(f\"... (truncated at {LIMIT} rows)\")
except Exception as e:
    print(\"ERROR:\", e)
PYEOF"
