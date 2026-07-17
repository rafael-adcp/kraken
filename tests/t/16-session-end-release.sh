#!/usr/bin/env bash
# The SessionEnd auto-release hook: when a Claude Code session ends while a claim
# state file is present, the bundled hook runs `kraken.py release` so the task
# requeues in seconds instead of waiting ~6h for the reaper. It runs `kraken.py
# release`, so the released marker lands before in-progress drops (the claim
# window closes, §9).
# With no state file it is a strict no-op. It is best-effort: a failed release
# just falls back to the reaper and never blocks session exit (always exits 0).
. "$ROOT/tests/lib.sh"

HOOK="$ROOT/hooks/session-end-release.sh"
export KRAKEN_STATE_DIR="$STATE/kraken"

# --- session ends WITH a claim open: kraken.py release drives the requeue -----
mk_issue 7 "abandoned-on-exit task" kraken-task "project:app"
python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1 >/dev/null
[ -f "$KRAKEN_STATE_DIR/claim-w1.json" ] || fail "setup: claim did not write state file"
before="$(comment_count 7)"

# The hook is a command hook: Claude Code pipes a JSON event on stdin. Feed a
# representative SessionEnd payload and assert on the observable effects.
out="$(printf '%s' '{"hook_event_name":"SessionEnd","reason":"exit"}' | bash "$HOOK" 2>&1)"
assert_rc $? 0 "hook must never block session exit (exit 0)"

has_label 7 in-progress && fail "hook did not drop in-progress"
[ -f "$KRAKEN_STATE_DIR/claim-w1.json" ] && fail "hook did not delete the state file"
c="$(last_comment 7)"
printf '%s' "$c" | grep -qF '<!-- kraken {"type":"released","worker":"w1","reason":"session ended"} -->' \
  || fail "hook did not post the released marker via kraken.py release"
[ "$(comment_count 7)" -gt "$before" ] || fail "hook posted no release comment"

# The released task is claimable again — end to end, proving the window closed.
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w2)"
assert_rc $? 0 "task re-claimable after the hook released it"

# --- no state file: strict no-op (no writes at all) --------------------------
rm -rf "$KRAKEN_STATE_DIR"
mk_issue 8 "untouched task" kraken-task "project:app" in-progress
mk_comment 8 '<!-- kraken {"type":"claim","worker":"someone-else"} -->'
before8="$(comment_count 8)"
out="$(printf '%s' '{"hook_event_name":"SessionEnd","reason":"exit"}' | bash "$HOOK" 2>&1)"
assert_rc $? 0 "no-op hook still exits 0"
has_label 8 in-progress || fail "no-op hook wrongly removed a label"
assert_eq "$(comment_count 8)" "$before8" "no-op hook wrongly posted a comment"

# --- best-effort: a failing release never fails the hook ---------------------
mk_issue 9 "release-fails task" kraken-task "project:app"
python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 9 w3 >/dev/null
[ -f "$KRAKEN_STATE_DIR/claim-w3.json" ] || fail "setup: claim did not write state file for w3"
# Force every gh call inside kraken.py release to fail; the hook must still exit 0.
out="$(printf '%s' '{"hook_event_name":"SessionEnd","reason":"exit"}' \
  | GH_STUB_FAIL='.' bash "$HOOK" 2>&1)"
assert_rc $? 0 "hook stays exit 0 even when kraken.py release fails"

exit 0
