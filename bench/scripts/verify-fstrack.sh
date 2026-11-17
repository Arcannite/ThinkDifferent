#!/usr/bin/env bash
# Local smoke test for harness/fstrack/fstrackd.py. No docker or model needed.
# Covers: base/fs-change/tool_result/turn_end/final, exclusions, non-regular
# files, the large-file manifest, the repo-size circuit breaker and full mode.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
TMP=$(mktemp -d)
PID=""
cleanup() {
  if [ -n "$PID" ]; then kill -TERM "$PID" 2>/dev/null || true; wait "$PID" 2>/dev/null || true; fi
  rm -rf "$TMP"
}
trap cleanup EXIT

wait_for_lines() { # wait_for_lines <file> <n>
  local file=$1 n=${2:-1}
  for _ in $(seq 1 200); do
    [ -f "$file" ] && [ "$(wc -l < "$file")" -ge "$n" ] && return 0
    sleep 0.05
  done
  echo "FAIL: timed out waiting for $file ($n lines)" >&2
  return 1
}

start() { # start <app> <out> [env...]
  local app=$1 out=$2
  shift 2
  mkdir -p "$app" "$out"
  env FSTRACK_OUT="$out" FSTRACK_ROOTS="$app" FSTRACK_SCAN_INTERVAL=0.2 "$@" \
    python3 "$ROOT/harness/fstrack/fstrackd.py" >"$out/daemon.log" 2>&1 &
  PID=$!
  wait_for_lines "$out/index.jsonl" 1
}

stop() {
  kill -TERM "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true
  PID=""
}

gitdir_of() {
  python3 - "$1/summary.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
print(next(iter(d["roots"].values()))["gitdir"])
PY
}

in_head() { git --git-dir="$1" ls-tree -r --name-only HEAD | grep -qx "$2"; }

# ---------------------------------------------------------------- pass 1
# manifest mode: 2 MiB file is not stored, FIFO is not stored, small file is.
APP=$TMP/manifest-app
OUT=$TMP/manifest-out
mkdir -p "$APP"
printf 'hello\n' > "$APP/a.txt"
mkdir -p "$APP/node_modules/pkg" && printf 'ignored\n' > "$APP/node_modules/pkg/index.js"
dd if=/dev/urandom of="$APP/big.bin" bs=1M count=2 status=none
mkfifo "$APP/pipe.fifo"

start "$APP" "$OUT" FSTRACK_MAX_FILE_MB=1 FSTRACK_LARGE_MODE=manifest FSTRACK_MAX_REPO_MB=64 FSTRACK_HASH_LARGE=1
G=$(gitdir_of "$OUT")
LM=$(ls "$OUT"/*.large.jsonl)

in_head "$G" a.txt || { echo "FAIL: a.txt not tracked"; exit 1; }
in_head "$G" big.bin && { echo "FAIL: big.bin stored despite max_file_mb=1"; exit 1; }
in_head "$G" pipe.fifo && { echo "FAIL: FIFO stored"; exit 1; }
in_head "$G" node_modules/pkg/index.js && { echo "FAIL: node_modules stored"; exit 1; }
grep -q '"state": "large"' "$LM" || { echo "FAIL: big.bin missing from large manifest"; exit 1; }
grep -q '"sha256"' "$LM" || { echo "FAIL: hash_large_files did not hash big.bin"; exit 1; }

# A change to only the large file must update the manifest, not the git content.
index_before=$(wc -l < "$OUT/index.jsonl")
manifest_before=$(wc -l < "$LM")
dd if=/dev/urandom of="$APP/big.bin" bs=1M count=1 seek=0 conv=notrunc status=none
wait_for_lines "$LM" $((manifest_before + 1))
[ "$(wc -l < "$OUT/index.jsonl")" -eq "$index_before" ] || {
  echo "FAIL: large-file-only change created a content commit"; exit 1;
}

# Small-file change caught by the periodic scan.
printf 'world\n' >> "$APP/a.txt"
wait_for_lines "$OUT/index.jsonl" $((index_before + 1))
git --git-dir="$G" show HEAD:a.txt | grep -qx world || { echo "FAIL: a.txt change"; exit 1; }

# Tool result event and turn marker.
printf 'tool\n' >> "$APP/a.txt"
printf '%s\n' '{"kind":"tool_result","tool":"edit","toolCallId":"call_1","turn":1}' >> "$OUT/events.jsonl"
for _ in $(seq 1 200); do
  git --git-dir="$G" log --format=%s -n 20 | grep -q '^fstrack .* tool_result$' && break
  sleep 0.05
done
git --git-dir="$G" log --format=%s -n 20 | grep -q '^fstrack .* tool_result$' || {
  echo "FAIL: tool_result commit missing"; exit 1;
}
printf '%s\n' '{"kind":"turn_end","turn":1}' >> "$OUT/events.jsonl"
for _ in $(seq 1 200); do git --git-dir="$G" tag -l fstrack/turn-0001 | grep -q . && break; sleep 0.05; done
git --git-dir="$G" tag -l fstrack/turn-0001 | grep -q . || { echo "FAIL: turn tag missing"; exit 1; }
stop

python3 - "$OUT/summary.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
st = next(iter(d["roots"].values()))
assert d["policy"]["large_file_mode"] == "manifest", d["policy"]
assert d["policy"]["max_file_mb"] == 1, d["policy"]
assert st["over_cap"] is False, st
assert st["large_files"] == 1, st
assert st["non_regular"] == 1, st
assert st["large_bytes"] == 2 * 1024 * 1024, st
print("manifest policy OK:", d["policy"], "large_files=", st["large_files"])
PY

# ---------------------------------------------------------------- pass 2
# max_repo_mb circuit breaker: 2 MiB of candidates under a 1 MiB cap means no
# content may enter git, but empty turn markers still work.
APP=$TMP/cap-app
OUT=$TMP/cap-out
mkdir -p "$APP"
printf 'small\n' > "$APP/a.txt"
dd if=/dev/urandom of="$APP/big.bin" bs=1M count=2 status=none
start "$APP" "$OUT" FSTRACK_MAX_FILE_MB=8 FSTRACK_LARGE_MODE=manifest FSTRACK_MAX_REPO_MB=1
G=$(gitdir_of "$OUT")
[ -z "$(git --git-dir="$G" ls-tree -r --name-only HEAD)" ] || {
  echo "FAIL: content committed over max_repo_mb"; exit 1;
}
printf '%s\n' '{"kind":"turn_end","turn":1}' >> "$OUT/events.jsonl"
for _ in $(seq 1 200); do git --git-dir="$G" tag -l fstrack/turn-0001 | grep -q . && break; sleep 0.05; done
git --git-dir="$G" tag -l fstrack/turn-0001 | grep -q . || {
  echo "FAIL: empty turn marker missing while over cap"; exit 1;
}
stop
python3 - "$OUT/summary.json" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
st = next(iter(d["roots"].values()))
assert st["over_cap"] is True, st
assert st["repo_kb"] < 64, st
print(f"repo cap OK: over_cap=True repo_kb={st['repo_kb']} KiB")
PY

# ---------------------------------------------------------------- pass 3
# full mode stores the 2 MiB file even though max_file_mb=1.
APP=$TMP/full-app
OUT=$TMP/full-out
mkdir -p "$APP"
printf 'small\n' > "$APP/a.txt"
dd if=/dev/urandom of="$APP/big.bin" bs=1M count=2 status=none
start "$APP" "$OUT" FSTRACK_MAX_FILE_MB=1 FSTRACK_LARGE_MODE=full FSTRACK_MAX_REPO_MB=64
G=$(gitdir_of "$OUT")
in_head "$G" big.bin || { echo "FAIL: full mode did not store big.bin"; exit 1; }
stop
echo "full mode OK"

# ---------------------------------------------------------------- pass 4
# skip mode: the large file is neither stored nor manifested.
APP=$TMP/skip-app
OUT=$TMP/skip-out
mkdir -p "$APP"
printf 'small\n' > "$APP/a.txt"
dd if=/dev/urandom of="$APP/big.bin" bs=1M count=2 status=none
start "$APP" "$OUT" FSTRACK_MAX_FILE_MB=1 FSTRACK_LARGE_MODE=skip FSTRACK_MAX_REPO_MB=64
G=$(gitdir_of "$OUT")
in_head "$G" a.txt || { echo "FAIL: skip mode lost a.txt"; exit 1; }
in_head "$G" big.bin && { echo "FAIL: skip mode stored big.bin"; exit 1; }
[ -e "$OUT"/*.large.jsonl ] && { echo "FAIL: skip mode wrote a manifest"; exit 1; }
stop
echo "skip mode OK"

echo "PASS: fstrack guardrails smoke test"
