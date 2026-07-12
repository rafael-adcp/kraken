#!/usr/bin/env bash
# THE claim-race test: two workers claim the same issue concurrently, both
# passing the label guard (the stub's barrier holds each one at the guard read
# until both have arrived — the worst-case interleaving, deterministically).
# The invariant under test: EXACTLY one exits 0, the other exits 10 and removes
# nothing, and the winner is whoever's claimed-by: comment landed first.
. "$ROOT/tests/lib.sh"

mk_issue 7 "contested task" kraken-task "project:app"
export GH_STUB_BARRIER=2

bash "$SCRIPTS/claim.sh" OWNER/tasks 7 w-a >/dev/null 2>&1 &
pid_a=$!
bash "$SCRIPTS/claim.sh" OWNER/tasks 7 w-b >/dev/null 2>&1 &
pid_b=$!
wait "$pid_a"; rc_a=$?
wait "$pid_b"; rc_b=$?
unset GH_STUB_BARRIER

# Exactly one winner, one back-off — never zero, never two.
rcs="$(printf '%s\n%s\n' "$rc_a" "$rc_b" | sort -n | tr '\n' ' ')"
assert_eq "$rcs" "0 10 " "race exit codes (exactly one 0 and one 10)"

# The winner is the first claimed-by: in server order.
first="$(grep -h '^claimed-by: ' "$STATE/issues/7/comments/"*.md | head -1)"
if [ "$rc_a" -eq 0 ]; then winner=w-a; else winner=w-b; fi
assert_eq "$first" "claimed-by: $winner" "winner matches first claimed-by comment"

# The loser backed off without removing anything.
has_label 7 in-progress || fail "in-progress label missing after race"
assert_eq "$(comment_count 7)" "2" "both claim comments preserved"
grep -q 'remove-label' "$STATE/log" && fail "a racer removed a label while backing off"
exit 0
