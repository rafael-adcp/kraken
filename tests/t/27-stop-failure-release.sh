#!/usr/bin/env bash
# The StopFailure auto-release hook (matcher rate_limit in hooks.json): a usage
# limit kills the turn but not the session, so SessionEnd never fires — this
# hook is what releases the held claim (task requeues in seconds instead of
# waiting ~6h for the reaper) and stamps the wake-retry flag `kraken.py watch`
# reads to re-emit the wake the dead turn consumed. It runs `kraken.py
# release`, so the released marker lands before in-progress drops (the claim
# window closes, §9). Best-effort: it always exits 0 — a failed release falls
# back to the reaper, but the flag must be stamped regardless.
. "$ROOT/tests/lib.sh"

HOOK="$ROOT/hooks/stop-failure-release.sh"
export KRAKEN_STATE_DIR="$STATE/kraken"

# --- limit hits WITH a claim open: release + flag ----------------------------
mk_issue 7 "limit-struck task" kraken-task "project:app"
python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w1 >/dev/null
[ -f "$KRAKEN_STATE_DIR/claim-w1.json" ] || fail "setup: claim did not write state file"
before="$(comment_count 7)"

# The hook is a command hook: Claude Code pipes a JSON event on stdin. Feed a
# representative StopFailure payload and assert on the observable effects.
out="$(printf '%s' '{"hook_event_name":"StopFailure","error_type":"rate_limit"}' | bash "$HOOK" 2>&1)"
assert_rc $? 0 "hook must always exit 0"

has_label 7 in-progress && fail "hook did not drop in-progress"
[ -f "$KRAKEN_STATE_DIR/claim-w1.json" ] && fail "hook did not delete the state file"
c="$(last_comment 7)"
printf '%s' "$c" | grep -qF '<!-- kraken {"type":"released","worker":"w1","reason":"usage limit"} -->' \
  || fail "hook did not post the released marker (reason: usage limit) via kraken.py release"
[ "$(comment_count 7)" -gt "$before" ] || fail "hook posted no release comment"
[ -f "$KRAKEN_STATE_DIR/wake-retry" ] || fail "hook did not stamp the wake-retry flag"

# The released task is claimable again — end to end, proving the window closed.
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 w2)"
assert_rc $? 0 "task re-claimable after the hook released it"

# --- no claim open: no queue writes, but the flag still lands ----------------
# A limit can hit between tasks or on a watcher wake turn — the consumed wake
# must still be retried, so the flag is stamped even when there is nothing to
# release.
rm -rf "$KRAKEN_STATE_DIR"
mk_issue 8 "untouched task" kraken-task "project:app" in-progress
mk_comment 8 '<!-- kraken {"type":"claim","worker":"someone-else"} -->'
before8="$(comment_count 8)"
out="$(printf '%s' '{"hook_event_name":"StopFailure","error_type":"rate_limit"}' | bash "$HOOK" 2>&1)"
assert_rc $? 0 "claimless hook still exits 0"
has_label 8 in-progress || fail "claimless hook wrongly removed a label"
assert_eq "$(comment_count 8)" "$before8" "claimless hook wrongly posted a comment"
[ -f "$KRAKEN_STATE_DIR/wake-retry" ] || fail "claimless hook did not stamp the wake-retry flag"

# --- best-effort: a failing release never fails the hook, flag still lands ---
mk_issue 9 "release-fails task" kraken-task "project:app"
python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 9 w3 >/dev/null
[ -f "$KRAKEN_STATE_DIR/claim-w3.json" ] || fail "setup: claim did not write state file for w3"
rm -f "$KRAKEN_STATE_DIR/wake-retry"
# Force every gh call inside kraken.py release to fail; the hook must still
# exit 0 and still stamp the flag (it stamps before releasing on purpose).
out="$(printf '%s' '{"hook_event_name":"StopFailure","error_type":"rate_limit"}' \
  | GH_STUB_FAIL='.' bash "$HOOK" 2>&1)"
assert_rc $? 0 "hook stays exit 0 even when kraken.py release fails"
[ -f "$KRAKEN_STATE_DIR/wake-retry" ] || fail "flag must land even when the release fails"

exit 0
