#!/usr/bin/env bash
# kraken.py list-startable must never silently truncate the queue. The shared
# `gh issue list` call once passed `--limit 100`; gh page-fetches newest-first
# up to that ceiling and stops, so an over-100 queue silently lost its OLDEST
# tasks — the drain never saw them, "oldest first" sorted a truncated set, and
# the watcher diffed a truncated snapshot. The gh-stub honors `--limit`
# (returns the newest N), so this case fails against a 100-cap and passes once
# the ceiling is lifted.
#
# The truncation is in the fetch, before any startable/held split, so the seam
# is the fetch size — not the (per-candidate, expensive) blocked-by check. We
# therefore fill the queue past 100 with cheap held tasks (held rows skip the
# blocked-by check entirely) and plant the startable tasks among the OLDEST
# numbers — exactly the ones newest-first truncation drops first. If they still
# list, the fetch saw the whole queue.
. "$ROOT/tests/lib.sh"

# Three genuinely-startable tasks at the oldest end of the queue...
mk_issue 1 "oldest startable"  kraken-task "project:app"
mk_issue 2 "second startable"  kraken-task "project:app"
mk_issue 3 "third startable"   kraken-task "project:app"

# ...then 102 held tasks (younger numbers) that push the queue past 100 (105
# total). Held rows are cheap: the script short-circuits them before any gh api
# call. Under `--limit 100` the newest 100 held tasks fill the page and the
# three oldest startable ones fall off the end.
for n in $(seq 100 201); do
  mk_issue "$n" "held $n" kraken-task "project:app" in-progress
done

# Default mode: the three oldest startable tasks must survive. Under the old
# `--limit 100`, newest-first truncation keeps the 100 youngest held tasks and
# drops these three entirely -> empty output.
out="$(python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app)"
assert_rc $? 0 "default mode exit (queue > 100)"
expected="$(printf '1\toldest startable\n2\tsecond startable\n3\tthird startable')"
assert_eq "$out" "$expected" \
  "oldest startable tasks survive a >100 queue (would vanish under --limit 100)"

# Snapshot mode shares the same query, so it inherits the fix: all 105 open
# tasks appear, none silently dropped at the 100 boundary.
snap="$(python3 "$SCRIPTS/kraken.py" list-startable OWNER/tasks app --snapshot)"
assert_rc $? 0 "snapshot mode exit (queue > 100)"

snap_lines="$(printf '%s\n' "$snap" | grep -c '.')"
assert_eq "$snap_lines" "105" "snapshot lists the whole queue (3 startable + 102 held), not a truncated 100"

startable="$(printf '%s\n' "$snap" | grep -c ':startable$')" || true
assert_eq "$startable" "3" "all three oldest startable tasks reported startable in the full snapshot"
