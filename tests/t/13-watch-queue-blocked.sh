#!/usr/bin/env bash
# kraken.py watch's entire emit gate is `count > 0` on
# `kraken.py list-startable --snapshot`'s ":startable$" lines (it has
# no filter logic of its own since this task made list-startable the sole
# owner of "startable"). So proving a blocked-only queue's snapshot carries
# zero ":startable" lines IS the no-wake proof — that count is the exact
# expression watch's loop tests every poll. (Driving the loop itself
# needs real backgrounding + signal delivery across nested bash processes,
# which is not reliable in this harness/OS combination — the stub-based
# snapshot check is the deterministic seam.)
. "$ROOT/tests/lib.sh"

# The blocker lives in a different project — it is not itself a project:app
# candidate, so the project:app snapshot below is genuinely blocked-only.
mk_issue 1 "blocker (other project)" kraken-task "project:other"
mk_issue 2 "blocked candidate"       kraken-task "project:app"
mk_blocked_by 2 1

snapshot="$(python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot exit (blocked-only queue)"
assert_eq "$snapshot" "2:held" "blocked-only queue snapshot has no startable line"

startable_count="$(printf '%s\n' "$snapshot" | grep -c ':startable$')" || true
assert_eq "$startable_count" "0" \
  "blocked-only queue: zero startable lines (watch-queue's exact emit gate — this is what keeps it from waking)"

# Closing the blocker flips the candidate to startable in the snapshot — the
# change watch-queue's loop compares against its previous snapshot and wakes on.
echo "closed" > "$STATE/issues/1/state"
snapshot="$(python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot exit (blocker closed)"
assert_eq "$snapshot" "2:startable" "closing the blocker flips the candidate to startable"

startable_count="$(printf '%s\n' "$snapshot" | grep -c ':startable$')" || true
assert_eq "$startable_count" "1" "once the blocker closes, the snapshot has exactly the newly-clear task startable"

# The watch gate is textually verifiable too. `watch` lives in kraken.py, so
# assert against the module: it must not carry the false-alarm re-emission timer
# this task removed, and it must gate emission on count>0 AND the snapshot
# changing — nothing else.
grep -q 'REMIND_SECONDS' "$SCRIPTS/kraken.py" && fail "kraken.py watch must not retain the 30-min re-emission safety net"
grep -q 'count > 0 and snapshot != prev' "$SCRIPTS/kraken.py" \
  || fail "kraken.py watch must gate emission on count>0 AND snapshot changed, nothing else"
