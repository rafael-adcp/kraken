#!/usr/bin/env bash
# kraken.py claim-next: the deterministic list -> guard -> claim -> arbitrate
# loop collapsed into one invocation. Pins the end-to-end behavior against the
# gh stub: the clean win (oldest startable claimed, number/title/body printed),
# held-skip, the honest empty result (exit 3), and THE race — two workers
# running claim-next concurrently claim two DIFFERENT tasks, never the same one.
. "$ROOT/tests/lib.sh"

# Keep the best-effort claim-state file out of the real home dir.
export KRAKEN_STATE_DIR="$STATE/kraken-state"

tab_line() { printf '%s\t%s' "$1" "$2"; }

# --- 1. clean win: the oldest startable is claimed, its briefing printed -----
mk_issue 7 "oldest task"  kraken-task "project:app"
mk_issue 9 "younger task" kraken-task "project:app"
mk_body 7 $'### Goal\nship it'

out="$(python3 "$SCRIPTS/kraken.py" claim-next OWNER/tasks app w1)"
assert_rc $? 0 "clean claim-next exit"
printf '%s\n' "$out" | grep -qxF 'claim-next: claimed issue=7 worker=w1' \
  || fail "claim-next result line missing"
printf '%s\n' "$out" | grep -qxF -- "$(tab_line 7 'oldest task')" \
  || fail "number+title line missing"
printf '%s\n' "$out" | grep -qF 'ship it' || fail "issue body not emitted"
has_label 7 in-progress || fail "in-progress not added to the claimed task"
has_label 9 in-progress && fail "the younger task was wrongly claimed"
assert_eq "$(comment_count 7)" "1" "exactly one claim comment on the won task"
assert_disclaimer 7 w1

# --- 2. held-skip: #7 is now in-progress, so claim-next moves on to #9 -------
out="$(python3 "$SCRIPTS/kraken.py" claim-next OWNER/tasks app w2)"
assert_rc $? 0 "claim-next skips held, claims the next candidate"
printf '%s\n' "$out" | grep -qxF 'claim-next: claimed issue=9 worker=w2' \
  || fail "expected #9 claimed after skipping held #7"
has_label 9 in-progress || fail "#9 not claimed"

# --- 3. honest empty: both tasks held now -> distinct exit 3, nothing written -
before="$(comment_count 7)"
out="$(python3 "$SCRIPTS/kraken.py" claim-next OWNER/tasks app w3)"
assert_rc $? 3 "claim-next exit on an empty (nothing startable) queue"
assert_eq "$out" "claim-next: none project:app" "none machine line"
assert_eq "$(comment_count 7)" "$before" "no write on an empty queue"

# --- 4. JSON mode: the win is emitted as a structured object (last line) -----
mk_issue 12 "json task" kraken-task "project:jsonp"
mk_body 12 $'### Goal\njson body'
out="$(python3 "$SCRIPTS/kraken.py" claim-next OWNER/tasks jsonp w-json --json)"
assert_rc $? 0 "claim-next --json exit"
last="$(printf '%s\n' "$out" | tail -1)"
printf '%s' "$last" | jq -e '.issue == 12 and .title == "json task"' >/dev/null \
  || fail "claim-next --json payload wrong: $last"

# --- 5. THE race: two workers, two startable tasks, two DIFFERENT claims -----
mk_issue 20 "race oldest"  kraken-task "project:race"
mk_issue 22 "race younger" kraken-task "project:race"

python3 "$SCRIPTS/kraken.py" claim-next OWNER/tasks race w-a >"$STATE/a.out" 2>&1 &
pid_a=$!
python3 "$SCRIPTS/kraken.py" claim-next OWNER/tasks race w-b >"$STATE/b.out" 2>&1 &
pid_b=$!
wait "$pid_a"; rc_a=$?
wait "$pid_b"; rc_b=$?

assert_rc "$rc_a" 0 "worker A won a task in the race"
assert_rc "$rc_b" 0 "worker B won a task in the race"

# The `claim-next: claimed issue=N` line is each worker's own final win — the
# per-attempt `claim:` lines above it may name a task it lost, so anchor on the
# claim-next line, not on any `claimed issue=`.
a_issue="$(grep -oE '^claim-next: claimed issue=[0-9]+' "$STATE/a.out" | grep -oE '[0-9]+$')"
b_issue="$(grep -oE '^claim-next: claimed issue=[0-9]+' "$STATE/b.out" | grep -oE '[0-9]+$')"
[ -n "$a_issue" ] || fail "worker A printed no claim-next win"
[ -n "$b_issue" ] || fail "worker B printed no claim-next win"
[ "$a_issue" != "$b_issue" ] || fail "both workers claimed the SAME task (#$a_issue)"

# Both race tasks ended up claimed — one per worker, no double-claim, no stray.
has_label 20 in-progress || fail "#20 was not claimed by either worker"
has_label 22 in-progress || fail "#22 was not claimed by either worker"

exit 0
