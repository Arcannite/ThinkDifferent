#!/usr/bin/env bash
# Build harness/bundle/ from sources already present on this machine.
#
#   node   <- newest ~/.nvm/versions/node/v*        (must be >= 22.23.4: older Node
#             trusts the revoked emSign root and would expose API keys to the MITM)
#   pi     <- newest ~/.pi/agent/install/releases/*  (managed pi installation)
#   uv/uvx <- ~/.local/bin
#   python <- ~/.local/share/uv/python (uv-managed CPython, for offline uvx)
#   wheels <- pre-downloaded common verifier wheels
#
# Override any source with NODE_SRC / PI_SRC / UV_SRC / PY_SRC, e.g. to reproduce a
# bundle built on another machine.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
BUNDLE="$ROOT/harness/bundle"
STAGE="$ROOT/work/bundle-stage"

# Default sources are the newest local installs. Pin NODE_SRC explicitly when comparing
# runs across machines: Node version is load-bearing for TLS trust (see README).
latest_dir() { ls -d "$@" 2>/dev/null | sort -V | tail -1 || true; }
NODE_SRC=${NODE_SRC:-$(latest_dir "$HOME"/.nvm/versions/node/v*/)}
PI_SRC=${PI_SRC:-$(latest_dir "$HOME"/.pi/agent/install/releases/*/)}
NODE_SRC=${NODE_SRC%/}
PI_SRC=${PI_SRC%/}
UV_SRC=${UV_SRC:-$HOME/.local/bin}
PY_SRC=${PY_SRC:-$(latest_dir "$HOME"/.local/share/uv/python/cpython-3.13.*-linux-x86_64-gnu/)}
PY_SRC=${PY_SRC%/}

say() { printf '== %s\n' "$*"; }
die() { printf 'build-bundle: %s\n' "$*" >&2; exit 1; }

[ -x "$NODE_SRC/bin/node" ] || die "node not found at $NODE_SRC"
[ -f "$PI_SRC/node_modules/@earendil-works/pi-coding-agent/package.json" ] || die "pi not found at $PI_SRC"
[ -x "$UV_SRC/uv" ] || die "uv not found at $UV_SRC"

NODE_V=$("$NODE_SRC/bin/node" -v)
PI_V=$("$NODE_SRC/bin/node" -e \
  "console.log(require('$PI_SRC/node_modules/@earendil-works/pi-coding-agent/package.json').version)")
say "node $NODE_V  pi $PI_V  uv $("$UV_SRC/uv" --version | awk '{print $2}')"

# ---------------------------------------------------------------- node tarball
say "staging node ($NODE_V)"
rm -rf "$STAGE"; mkdir -p "$STAGE/node"
cp -a "$NODE_SRC/bin" "$STAGE/node/"
for d in include lib share; do [ -d "$NODE_SRC/$d" ] && cp -a "$NODE_SRC/$d" "$STAGE/node/"; done
cp -a "$NODE_SRC/LICENSE" "$STAGE/node/" 2>/dev/null || true

# Minimal container images (ubuntu:24.04, debian-slim) ship libgcc_s but not
# libatomic.so.1, and this Node build needs it. Ship it beside the binary and point the
# loader at it via a launcher, so install.sh never needs apt.
mkdir -p "$STAGE/node/lib/vendor"
for lib in libatomic.so.1; do
  src=$(ldconfig -p | awk -v l="$lib" '$1==l {print $NF; exit}')
  [ -n "$src" ] || die "cannot locate $lib on this machine"
  cp -L "$src" "$STAGE/node/lib/vendor/$lib"
done
mv "$STAGE/node/bin/node" "$STAGE/node/bin/node.real"
cat > "$STAGE/node/bin/node" <<'SH'
#!/bin/sh
# Launcher: supplies the shared libraries this Node build needs but minimal images omit.
# Resolves symlinks first, because install.sh exposes this as /usr/local/bin/node.
self=$0
while [ -L "$self" ]; do
  link=$(readlink "$self") || break
  case $link in
    /*) self=$link ;;
    *)  self=$(dirname "$self")/$link ;;
  esac
done
d=$(cd "$(dirname "$self")" && pwd)
root=$(dirname "$d")
LD_LIBRARY_PATH="$root/lib/vendor${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export LD_LIBRARY_PATH
exec "$d/node.real" "$@"
SH
chmod +x "$STAGE/node/bin/node" "$STAGE/node/bin/node.real"

# ---------------------------------------------------------------- pi tarball
say "staging pi ($PI_V)"
mkdir -p "$STAGE/pi/lib" "$STAGE/pi/bin"
cp -a "$PI_SRC/node_modules" "$STAGE/pi/lib/node_modules"
# drop the install-root lockfile metadata: containers get models.json + settings.json
rm -f "$STAGE/pi/lib/node_modules/.package-lock.json"

# bin/pi resolves its own location, so it works from any prefix.
cat > "$STAGE/pi/bin/pi" <<'SH'
#!/bin/sh
# Self-locating pi launcher: <prefix>/bin/pi -> <prefix>/lib/node_modules/...
# Resolves symlinks first, because install.sh exposes this as /usr/local/bin/pi.
self=$0
while [ -L "$self" ]; do
  link=$(readlink "$self") || break
  case $link in
    /*) self=$link ;;
    *)  self=$(dirname "$self")/$link ;;
  esac
done
here=$(cd "$(dirname "$self")" && pwd)
prefix=$(dirname "$here")
node_bin=${PI_NODE_BIN:-/opt/node/bin/node}
[ -x "$node_bin" ] || node_bin=$(command -v node)
exec "$node_bin" "$prefix/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js" "$@"
SH
chmod +x "$STAGE/pi/bin/pi"

# ---------------------------------------------------------------- uv + uvx
say "staging uv/uvx"
mkdir -p "$STAGE/uv/uv-x86_64-unknown-linux-gnu"
cp "$UV_SRC/uv" "$UV_SRC/uvx" "$STAGE/uv/uv-x86_64-unknown-linux-gnu/"
chmod +x "$STAGE/uv/uv-x86_64-unknown-linux-gnu/"*

# ---------------------------------------------------------------- python
mkdir -p "$STAGE/python"
if [ -d "$PY_SRC" ]; then
  say "staging uv-managed CPython $(basename "$PY_SRC")"
  cp -a "$PY_SRC" "$STAGE/python/"
else
  say "WARNING: no uv-managed CPython at $PY_SRC; uvx will need network"
fi

# ---------------------------------------------------------------- wheels
say "staging verifier wheelhouse"
mkdir -p "$STAGE/wheels"
# The two the TB4 verifiers use most (20 and 19 of 66 tasks). Task-specific wheels are
# not pre-fetched: those verifier images install their own deps with network.
WHEELS="pytest==9.1.1 pytest-json-ctrf==0.5.2 pyyaml==6.0.2 wheel==0.47.0"
# uv has no `pip download` subcommand; use a throwaway pip.
if ! "$UV_SRC/uv" tool run --from pip pip download --quiet --no-deps \
     --dest "$STAGE/wheels" $WHEELS 2>/dev/null; then
  say "WARNING: wheel download failed; wheels/ will be empty"
fi

# ---------------------------------------------------------------- meta files
cat > "$STAGE/VERSIONS" <<EOF
node=$(echo "$NODE_V" | tr -d v)
pi=$(echo "$PI_V")
uv=$("$UV_SRC/uv" --version | awk '{print $2}')
python=$(basename "${PY_SRC:-none}")
built_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
built_from=local-machine
EOF

cat > "$STAGE/settings.json" <<'JSON'
{
  "enableInstallTelemetry": false,
  "enableAnalytics": false,
  "autoUpdate": false
}
JSON

cp "$ROOT/scripts/bundle-install.sh" "$STAGE/install.sh"
chmod +x "$STAGE/install.sh"

# ---------------------------------------------------------------- pack
say "packing into $BUNDLE"
mkdir -p "$BUNDLE"
rm -f "$BUNDLE"/node-linux-x64.tar.gz "$BUNDLE"/pi-agent.tar.gz "$BUNDLE"/uv-*.tar.gz
tar -C "$STAGE" -czf "$BUNDLE/node-linux-x64.tar.gz" node
tar -C "$STAGE/pi" -czf "$BUNDLE/pi-agent.tar.gz" bin lib
tar -C "$STAGE/uv" -czf "$BUNDLE/uv-x86_64-unknown-linux-gnu.tar.gz" uv-x86_64-unknown-linux-gnu
rm -rf "$BUNDLE/wheels" "$BUNDLE/python"
cp -a "$STAGE/wheels" "$BUNDLE/wheels"
[ -d "$STAGE/python" ] && cp -a "$STAGE/python" "$BUNDLE/python"
cp "$STAGE/settings.json" "$STAGE/install.sh" "$STAGE/VERSIONS" "$BUNDLE/"

# models.json is generated by `bench render`, not by this script.
say "done"
ls -lh "$BUNDLE"
du -sh "$BUNDLE"