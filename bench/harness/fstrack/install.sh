#!/usr/bin/env bash
# Prepare a task container for fstrack. Runs as root from the pi adapter's
# install() phase, before the agent starts.
#
# - makes sure a usable `git` exists (task images vary; Debian slim images do
#   not ship it)
# - makes sure a usable Python 3 exists (system python3, else the offline
#   bundle's CPython)
# - creates the collected output directory with permissive modes so the agent
#   user can append events while the root-owned daemon writes the git repo
#
# Exits 0 even when tracking cannot be enabled: the benchmark trial must still
# run. The reason is written to /logs/artifacts/fstrack/install.log.
set -uo pipefail

OUT=${FSTRACK_OUT:-/logs/artifacts/fstrack}
mkdir -p "$OUT" 2>/dev/null || true
LOG="$OUT/install.log"
log() { printf 'fstrack install: %s\n' "$*" | tee -a "$LOG" >&2; }

# ------------------------------------------------------------------ git
if ! command -v git >/dev/null 2>&1; then
  log "git not found; trying the image's package manager"
  export DEBIAN_FRONTEND=noninteractive
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >>"$LOG" 2>&1 || true
    apt-get install -y -qq --no-install-recommends git >>"$LOG" 2>&1 || true
  elif command -v apk >/dev/null 2>&1; then
    apk add --no-cache git >>"$LOG" 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y git >>"$LOG" 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    yum install -y git >>"$LOG" 2>&1 || true
  fi
fi

if ! command -v git >/dev/null 2>&1; then
  log "ERROR: git is unavailable and could not be installed; tracking disabled"
  exit 0
fi

# ------------------------------------------------------------------ python
PY=""
if command -v python3 >/dev/null 2>&1; then
  PY=$(command -v python3)
fi
if [ -z "$PY" ]; then
  for candidate in /opt/uv-python/*/bin/python3 /opt/pi-bundle/python/*/bin/python3; do
    if [ -x "$candidate" ]; then
      PY=$candidate
      break
    fi
  done
fi
if [ -z "$PY" ]; then
  log "ERROR: no python3 and no bundled CPython; tracking disabled"
  exit 0
fi
mkdir -p /usr/local/bin
ln -sfn "$PY" /usr/local/bin/fstrack-python

# ---------------------------------------------------------------- output dir
mkdir -p "$OUT"
chmod 0777 /logs/artifacts "$OUT" 2>/dev/null || true
: >>"$OUT/events.jsonl"
chmod 0666 "$OUT/events.jsonl" 2>/dev/null || true

log "git=$(git --version 2>/dev/null || echo '?') python=$PY out=$OUT"
