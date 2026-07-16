#!/usr/bin/env bash
# The queue watcher's idle poll must cost O(1) gh calls, not O(N) in queue
# size. Before this, a snapshot ran one gh-stub-logged `dependencies/blocked_by`
# call per non-held task on top of the listing call — a 50-task queue was 51
# calls, scaling with every additional free task. This pins the invocation
# count against the gh-stub's log so that regression can't creep back in.
. "$ROOT/tests/lib.sh"

# 50 free (non-held, no native/depends-on blockers) tasks — the exact shape
# that used to cost one gh api call each on top of the listing call.
for n in $(seq 1 50); do
  mk_issue "$n" "task $n" kraken-task "project:app"
done

: > "$STATE/log"
out="$(python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot exit (50 free tasks)"
lines="$(printf '%s\n' "$out" | grep -c ':startable$')" || true
assert_eq "$lines" "50" "all 50 free tasks report startable"

calls="$(wc -l < "$STATE/log" | tr -d ' ')"
[ "$calls" -le 2 ] || fail "expected O(1) gh calls for 50 free tasks (single page), got $calls: $(cat "$STATE/log")"

# The depends-on fallback (only reached when a candidate's native blockedBy
# list comes back empty) must also stay O(1): 30 candidates all falling back
# to the same body-line check must not add one gh call each — they resolve in
# a single batched query, on top of the (unaffected) 50 free tasks above.
mk_issue 900 "dep target" kraken-task "project:app"
for n in $(seq 901 930); do
  mk_issue "$n" "fallback $n" kraken-task "project:app"
  mk_body "$n" "depends-on: #900"
done

: > "$STATE/log"
out="$(python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot exit (depends-on fan-out)"
lines="$(printf '%s\n' "$out" | grep -c '.')"
assert_eq "$lines" "81" "50 free + 1 open dep target + 30 fallback candidates, all reported"
held="$(printf '%s\n' "$out" | grep -c ':held$')" || true
assert_eq "$held" "30" "all 30 fallback candidates held while the dep target is open"

calls="$(wc -l < "$STATE/log" | tr -d ' ')"
[ "$calls" -le 3 ] || fail "expected O(1) gh calls for a 30-candidate depends-on fan-out, got $calls: $(cat "$STATE/log")"
