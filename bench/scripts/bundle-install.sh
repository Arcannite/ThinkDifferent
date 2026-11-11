#!/usr/bin/env bash
# Offline pi installer. Runs as root inside a task container with NO network.
# Idempotent: safe to run more than once.
#
# Installs:
#   /opt/node/bin/{node,npm,npx}                 node >= 22.23.4
#   /opt/pi/{bin/pi,lib/node_modules/...}        pi coding agent
#   /opt/uv/{uv,uvx}                             for verifiers
#   /opt/uv-python/                              uv-managed CPython for `uvx --offline`
#   /opt/wheels/                                 common verifier wheels
#   /usr/local/bin/{node,npm,npx,pi,uv,uvx}      on PATH for every user
set -euo pipefail

B=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
NODE_PREFIX=${NODE_PREFIX:-/opt/node}
PI_PREFIX=${PI_PREFIX:-/opt/pi}
UV_PREFIX=${UV_PREFIX:-/opt/uv}
UV_PYTHON_DIR=${UV_PYTHON_INSTALL_DIR:-/opt/uv-python}
WHEEL_DIR=${WHEEL_DIR:-/opt/wheels}

log() { printf 'install.sh: %s\n' "$*"; }

# ------------------------------------------------------------------ node
if [ ! -x "$NODE_PREFIX/bin/node" ]; then
  log "installing node -> $NODE_PREFIX"
  mkdir -p "$NODE_PREFIX"
  tar -xzf "$B/node-linux-x64.tar.gz" -C "$NODE_PREFIX" --strip-components=1
else
  log "node already present"
fi
export PATH="$NODE_PREFIX/bin:$PATH"

# ------------------------------------------------------------------ pi
if [ ! -f "$PI_PREFIX/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js" ]; then
  log "installing pi -> $PI_PREFIX"
  mkdir -p "$PI_PREFIX"
  tar -xzf "$B/pi-agent.tar.gz" -C "$PI_PREFIX"
else
  log "pi already present"
fi
# Compatibility path used by scripts/verify-bundle.sh and older runbooks.
PI_PKG="$PI_PREFIX/lib/node_modules/@earendil-works/pi-coding-agent"
ln -sfn bundle/cli.js "$PI_PKG/dist/cli.js" 2>/dev/null || true
chmod +x "$PI_PREFIX/bin/pi" 2>/dev/null || true

# ------------------------------------------------------------------ uv
if [ ! -x "$UV_PREFIX/uv" ]; then
  log "installing uv -> $UV_PREFIX"
  mkdir -p "$UV_PREFIX"
  tar -xzf "$B"/uv-*.tar.gz -C "$UV_PREFIX" --strip-components=1
  chmod +x "$UV_PREFIX/uv" "$UV_PREFIX/uvx"
else
  log "uv already present"
fi

# ------------------------------------------------------------------ python + wheels
if [ -d "$B/python" ] && [ -z "$(ls -A "$UV_PYTHON_DIR" 2>/dev/null || true)" ]; then
  log "installing bundled CPython -> $UV_PYTHON_DIR"
  mkdir -p "$UV_PYTHON_DIR"
  cp -a "$B/python/." "$UV_PYTHON_DIR/"
fi
if [ -d "$B/wheels" ] && [ -n "$(ls -A "$B/wheels" 2>/dev/null || true)" ]; then
  log "installing wheelhouse -> $WHEEL_DIR"
  mkdir -p "$WHEEL_DIR"
  cp -a "$B/wheels/." "$WHEEL_DIR/"
fi

# ------------------------------------------------------------------ PATH shims
mkdir -p /usr/local/bin
ln -sfn "$NODE_PREFIX/bin/node" /usr/local/bin/node
ln -sfn "$NODE_PREFIX/bin/npm"  /usr/local/bin/npm
ln -sfn "$NODE_PREFIX/bin/npx"  /usr/local/bin/npx
ln -sfn "$PI_PREFIX/bin/pi"     /usr/local/bin/pi
ln -sfn "$UV_PREFIX/uv"         /usr/local/bin/uv
ln -sfn "$UV_PREFIX/uvx"        /usr/local/bin/uvx

# ------------------------------------------------------------------ pi state
# Offline, no telemetry, no update pings. Applies to the root account and, when the
# installer is run with AGENT_HOME set, to the agent user's home too.
write_settings() {
  local home=$1
  [ -n "$home" ] || return 0
  mkdir -p "$home/.pi/agent"
  cp "$B/settings.json" "$home/.pi/agent/settings.json"
  [ -f "$B/models.json" ] && cp "$B/models.json" "$home/.pi/agent/models.json"
  return 0
}
write_settings "${ROOT_HOME:-/root}"
[ -n "${AGENT_HOME:-}" ] && write_settings "$AGENT_HOME"

log "node $(node -v)"
log "pi $(PI_OFFLINE=1 node "$PI_PKG/dist/cli.js" --version 2>/dev/null || echo '?')"
log "done"