#!/usr/bin/env bash
# Verify user-provided datasets. Fails loudly rather than guessing.
set -uo pipefail
D=${1:-$(cd "$(dirname "$0")/.." && pwd)/datasets}
fail=0
say(){ printf '%-46s %s\n' "$1" "$2"; }

tb4="$D/terminalbench4/tasks"
n=$(find "$tb4" -maxdepth 2 -name task.toml 2>/dev/null | wc -l | tr -d ' ')
[ "$n" = "66" ] && say "TB4 task count" "OK ($n)" || { say "TB4 task count" "FAIL (want 66, got ${n:-0})"; fail=1; }
[ -f "$D/terminalbench4/registry.json" ] && say "TB4 registry.json" OK || { say "TB4 registry.json" MISSING; fail=1; }
for f in task.toml instruction.md environment tests; do
  miss=$(find "$tb4" -maxdepth 2 -name "$f" 2>/dev/null | wc -l | tr -d ' ')
  [ "$miss" -ge 66 ] || { say "TB4 per-task $f" "FAIL (${miss})"; fail=1; }
done
say "TB4 size" "$(du -sh "$D/terminalbench4" 2>/dev/null | cut -f1)"

ds="$D/deepswe/tasks"
n=$(find "$ds" -maxdepth 2 -name task.toml 2>/dev/null | wc -l | tr -d ' ')
[ "$n" = "113" ] && say "DeepSWE task count" "OK ($n)" || { say "DeepSWE task count" "FAIL (want 113, got ${n:-0})"; fail=1; }
grep -rq 'public.ecr.aws/d3j8x8q7/swe-bench-202605' "$ds" 2>/dev/null \
  && say "DeepSWE prebuilt image refs" OK || { say "DeepSWE prebuilt image refs" "FAIL (partial clone?)"; fail=1; }
grep -rq 'environment_mode = "separate"' "$ds" 2>/dev/null \
  && say "DeepSWE v1.1 separate verifier" OK || { say "DeepSWE v1.1 separate verifier" "FAIL (old revision?)"; fail=1; }
[ -s "$D/deepswe.REVISION" ] && say "DeepSWE revision pinned" "$(cut -c1-12 "$D/deepswe.REVISION")" \
  || { say "DeepSWE revision pinned" MISSING; fail=1; }

exit $fail
