#!/usr/bin/env bash
# USER-RUN dataset fetch. The agent must NOT execute this (runbook rule 3):
# datasets are user-supplied and pinned by the user.
#
# Usage:  bash scripts/fetch-datasets.sh [terminalbench4|deepswe|all]
#
# Requires: hf (huggingface_hub CLI), git, git-lfs. Host egress must go through the
# local proxy because github.com / huggingface.co are MITM'd on this box.
set -euo pipefail

BENCH_ROOT=${BENCH_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}
PROXY=${PROXY:-http://127.0.0.1:7897}
export HTTPS_PROXY=$PROXY HTTP_PROXY=$PROXY
export ALL_PROXY=$PROXY
export GIT_TERMINAL_PROMPT=0

TB4_REV=v4.0.0
DEEPSWE_URL=https://github.com/datacurve-ai/deep-swe
WHAT=${1:-all}

fetch_tb4() {
  echo "== Terminal-Bench 4.0 @ ${TB4_REV}"
  hf download harborframework/terminal-bench \
    --repo-type dataset \
    --revision "$TB4_REV" \
    --local-dir "$BENCH_ROOT/datasets/terminalbench4"
}

fetch_deepswe() {
  echo "== DeepSWE v1.1 (datacurve-ai/deep-swe)"
  if [ -d "$BENCH_ROOT/datasets/deepswe/.git" ]; then
    git -C "$BENCH_ROOT/datasets/deepswe" -c http.proxy="$PROXY" fetch --all --tags
  else
    git -c http.proxy="$PROXY" clone --filter=blob:none "$DEEPSWE_URL" \
      "$BENCH_ROOT/datasets/deepswe"
  fi
  git -C "$BENCH_ROOT/datasets/deepswe" rev-parse HEAD \
    > "$BENCH_ROOT/datasets/deepswe.REVISION"
  echo "pinned: $(cat "$BENCH_ROOT/datasets/deepswe.REVISION")"
}

case "$WHAT" in
  terminalbench4|tb4) fetch_tb4 ;;
  deepswe)            fetch_deepswe ;;
  all)                fetch_tb4; fetch_deepswe ;;
  *) echo "usage: $0 [terminalbench4|deepswe|all]" >&2; exit 2 ;;
esac

echo
echo "== verify (read-only) =="
bash "$BENCH_ROOT/scripts/verify-datasets.sh" "$BENCH_ROOT/datasets"