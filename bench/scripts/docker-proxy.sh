#!/usr/bin/env bash
# Make Docker (build + run) use the loopback proxy that works on this network.
#
#   scripts/docker-proxy.sh status     # is it up?
#   scripts/docker-proxy.sh on         # start forwarder + write ~/.docker/config.json
#   scripts/docker-proxy.sh off        # stop forwarder + remove the proxies block
#
# Why this exists: github.com / huggingface.co are DNS-poisoned here and serve a
# self-signed chain, so image builds that curl GitHub fail with exit 60. Containers
# cannot reach 127.0.0.1:7897, hence the forwarder on the docker bridge address.
set -uo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
FWD="$ROOT/scripts/host-proxy-forwarder.py"
PIDFILE="$ROOT/work/docker-proxy.pid"
LOGFILE="$ROOT/logs/docker-proxy.log"
DOCKER_CFG="$HOME/.docker/config.json"
LISTEN_IP=172.17.0.1
LISTEN_PORT=7898
PROXY="http://${LISTEN_IP}:${LISTEN_PORT}"
# Hosts that resolve correctly and serve valid certs on this network: go direct so
# apt/pip/npm are not throttled by the proxy. Everything else (github.com,
# *.githubusercontent.com, huggingface.co, hf.co, astral.sh) is DNS-poisoned and
# must go through the proxy.
NO_PROXY_LIST="localhost,127.0.0.1,::1,172.17.0.1,deb.debian.org,security.debian.org,archive.ubuntu.com,security.ubuntu.com,ports.ubuntu.com,pypi.org,files.pythonhosted.org,registry.npmjs.org,nodejs.org,ollama.com,openrouter.ai,public.ecr.aws"

mkdir -p "$ROOT/work" "$ROOT/logs" "$HOME/.docker"

SERVICE=bench-docker-proxy.service
UNIT="$HOME/.config/systemd/user/$SERVICE"

install_unit() {
  command -v systemctl >/dev/null 2>&1 || return 0
  [ -f "$UNIT" ] && return 0
  mkdir -p "$(dirname "$UNIT")"
  cat > "$UNIT" <<EOF
[Unit]
Description=bench: expose the loopback HTTP proxy to Docker containers
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $FWD
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable "$SERVICE" 2>/dev/null || true
  echo "installed systemd --user unit: $UNIT"
}

systemd_active() {
  command -v systemctl >/dev/null 2>&1 &&
    systemctl --user is-active --quiet "$SERVICE" 2>/dev/null
}

running() {
  systemd_active || { [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; }
}

case "${1:-status}" in
  on)
    install_unit
    if systemd_active; then
      echo "forwarder already managed by systemd --user ($SERVICE)"
    elif ! running; then
      # Prefer the systemd unit: it survives this shell and restarts on failure.
      if command -v systemctl >/dev/null 2>&1; then
        systemctl --user start "$SERVICE" 2>/dev/null || true
      fi
      systemd_active || {
        setsid nohup python3 "$FWD" > "$LOGFILE" 2>&1 < /dev/null &
        echo $! > "$PIDFILE"
        sleep 0.5
      }
    fi
    running || { echo "forwarder failed to start; see $LOGFILE" >&2; exit 1; }
    python3 - "$DOCKER_CFG" "$PROXY" "$NO_PROXY_LIST" <<'PY'
import json, os, sys
path, proxy, no_proxy = sys.argv[1], sys.argv[2], sys.argv[3]
cfg = {}
if os.path.exists(path):
    try:
        cfg = json.load(open(path))
    except json.JSONDecodeError:
        raise SystemExit(f"{path} is not valid JSON; refusing to overwrite")
cfg["proxies"] = {"default": {
    "httpProxy": proxy, "httpsProxy": proxy, "noProxy": no_proxy}}
json.dump(cfg, open(path, "w"), indent=2)
open(path, "a").write("\n")
os.chmod(path, 0o600)
PY
    if systemd_active; then
      echo "docker proxy -> $PROXY (systemd --user $SERVICE)"
    else
      pid=$([ -f "$PIDFILE" ] && cat "$PIDFILE" 2>/dev/null || true)
      echo "docker proxy -> $PROXY${pid:+ (forwarder pid $pid)}"
    fi
    ;;
  off)
    if systemd_active; then systemctl --user stop "$SERVICE" 2>/dev/null; fi
    [ -f "$PIDFILE" ] && kill "$(cat "$PIDFILE")" 2>/dev/null
    rm -f "$PIDFILE"
    [ -f "$DOCKER_CFG" ] && python3 - "$DOCKER_CFG" <<'PY'
import json, sys
p = sys.argv[1]
try:
    cfg = json.load(open(p))
except Exception:
    raise SystemExit(0)
cfg.pop("proxies", None)
json.dump(cfg, open(p, "w"), indent=2)
open(p, "a").write("\n")
PY
    echo "docker proxy disabled"
    ;;
  status)
    if systemd_active; then
      echo "forwarder up (systemd --user $SERVICE: $UNIT) -> $PROXY"
    elif running; then
      echo "forwarder up (standalone pid $(cat "$PIDFILE" 2>/dev/null || echo '?')) -> $PROXY"
    else
      echo "forwarder down"
    fi
    [ -f "$DOCKER_CFG" ] && grep -q 7898 "$DOCKER_CFG" && echo "config.json: proxy set" || echo "config.json: no proxy"
    ;;
  *) echo "usage: $0 {on|off|status}" >&2; exit 2 ;;
esac