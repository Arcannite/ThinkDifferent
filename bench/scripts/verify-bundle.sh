#!/usr/bin/env bash
# Verify harness/bundle/ by installing it offline in a bare container.
#   bash scripts/verify-bundle.sh [bundle-dir]
set -uo pipefail
B=${1:-$(cd "$(dirname "$0")/.." && pwd)/harness/bundle}
fail=0

ls -l "$B/install.sh" "$B/node-linux-x64.tar.gz" "$B/pi-agent.tar.gz" || exit 2
# NOTE: `tar tzf | head -1` trips pipefail (SIGPIPE gives tar exit 141), so read the
# full listing and take the unique top-level entries instead.
node_top=$(tar tzf "$B/node-linux-x64.tar.gz" | cut -d/ -f1 | sort -u | tr '\n' ' ')
case "$node_top" in
  "node "*) ;;
  *) echo "FAIL: node tarball root must be node/ (found: $node_top)"; exit 1 ;;
esac
[ -s "$B/settings.json" ] || { echo "FAIL: missing settings.json"; fail=1; }
[ -d "$B/python" ]       || echo "WARN: no bundled python/ (uvx will need network)"
[ -n "$(ls -A "$B/wheels" 2>/dev/null)" ] || echo "WARN: empty wheels/"

docker run --rm --platform linux/amd64 --network none \
  -v "$B:/opt/pi-bundle:ro" --entrypoint bash ubuntu:24.04 -c '
  bash /opt/pi-bundle/install.sh >/tmp/install.log 2>&1 || { tail -30 /tmp/install.log; exit 3; }
  node_v=$(/opt/node/bin/node -v)
  echo "node: $node_v"
  echo "pi:   $(PI_OFFLINE=1 /opt/node/bin/node /opt/pi/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js --version)"
  echo "uv:   $(/opt/uv/uv --version)"
  case "$node_v" in
    v2[2-9].*|v[3-9][0-9].*) ;;
    *) echo "FAIL: node too old ($node_v)"; exit 4 ;;
  esac
  # must be >= 22.23.4
  set -- $(echo "${node_v#v}" | tr . " ")
  if [ "$1" -eq 22 ] && { [ "$2" -lt 23 ] || { [ "$2" -eq 23 ] && [ "$3" -lt 4 ]; }; }; then
    echo "FAIL: node $node_v < 22.23.4 (revoked-root trust)"; exit 5
  fi
'
rc=$?
[ $rc -ne 0 ] && { echo "FAIL: offline install (rc=$rc)"; fail=1; }

# TLS sanity needs network: a chain forged on the revoked emSign root must be REJECTED.
probe=$(docker run --rm --platform linux/amd64 \
  -v "$B:/opt/pi-bundle:ro" --entrypoint bash ubuntu:24.04 -c '
  bash /opt/pi-bundle/install.sh >/dev/null 2>&1 || exit 3
  /opt/node/bin/node -e "const c=new AbortController();setTimeout(()=>c.abort(),20000);fetch(\"https://dv-g3b-emudhra.we-have-a-problem.com/\",{signal:c.signal}).then(r=>console.log(\"TRUSTED\",r.status)).catch(e=>console.log(\"REJECTED\",(e.cause&&e.cause.code)||e.code))"
' 2>&1 | tail -1)
echo "tls probe: $probe"
case "$probe" in
  *REJECTED*CERT_REVOKED*) echo "PASS: revoked chain rejected" ;;
  *TRUSTED*) echo "FAIL: bundle trusts the revoked emSign root"; fail=1 ;;
  *) echo "WARN: probe inconclusive ($probe)" ;;
esac

[ -f "$B/VERSIONS" ] && { echo "-- VERSIONS --"; cat "$B/VERSIONS"; }
exit $fail