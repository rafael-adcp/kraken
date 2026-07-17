#!/usr/bin/env bash
# protocol/3 marker-only reading (PROTOCOL.md §4, issue #32): consumers read the
# hidden marker and NOTHING else. The retired protocol/1 visible line grammar is
# no longer parsed, so free text — even a line that starts with a former keyword
# (`released:`, `claimed-by:`, `delivered:`, …) — can never occupy a machine-line
# position. Driven end to end through the real kraken.py + gh-stub pipeline.
. "$ROOT/tests/lib.sh"

# --- 1. a bare former protocol/1 line is inert -------------------------------
# A thread whose ONLY "claim" is a retired `claimed-by:` line holds no live
# claim under protocol/3 — the line is prose. A fresh worker therefore WINS the
# (empty) window instead of losing to a phantom legacy claim.
mk_issue 7 "former protocol/1 claim line, now inert" kraken-task "project:app"
mk_comment 7 "claimed-by: ghost"

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 fresh)"
assert_rc $? 0 "a former claimed-by: line is not a claim"
assert_eq "$out" "claim: claimed issue=7 worker=fresh" \
  "protocol/1 line grammar is no longer read — the window was empty"

# --- 2. free text cannot forge a claim-window reset --------------------------
# owner holds a real claim marker. The worker then posts a heartbeat whose
# progress message is literally `released: owner` — attacker/innocent text that
# under protocol/1 would have reset the window. Under protocol/3 it is inert:
# owner still wins arbitration, so a challenger loses.
mk_issue 8 "free text cannot forge a reset" kraken-task "project:app"
mk_comment 8 '<!-- kraken {"type":"claim","worker":"owner"} -->'
python3 "$SCRIPTS/kraken.py" heartbeat OWNER/tasks 8 owner "released: owner" >/dev/null

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 8 challenger)"
assert_rc $? 10 "the free-text 'released: owner' line reset nothing"
assert_eq "$out" "claim: lost-tiebreaker issue=8 winner=owner" \
  "owner still owns the claim — free text is inert"

# --- 3. the produced comment shape is pinned ---------------------------------
# deliver with a result file whose free text CONTAINS colliding lines
# (`released: evil`, `claimed-by: evil`). The produced comment must be readable
# and safe: the disclaimer heads it, a blank line separates the sections, the
# free text is reproduced verbatim as prose, the machine payload rides exactly
# ONE hidden marker, and no other line is a kraken marker.
mk_issue 9 "delivered with colliding free text" kraken-task "project:app" in-progress
mk_comment 9 '<!-- kraken {"type":"claim","worker":"w1"} -->'

r="$STATE/result.md"
printf 'Shipped the feature.\n\nreleased: evil\nclaimed-by: evil\n' > "$r"

out="$(python3 "$SCRIPTS/kraken.py" deliver OWNER/tasks 9 w1 "$r")"
assert_rc $? 0 "deliver with colliding free text still succeeds"

c="$(last_comment 9)"
# disclaimer heads the comment, blank line follows (or GitHub folds the body in).
assert_disclaimer 9 w1
[ -z "$(printf '%s' "$c" | sed -n '2p')" ] || fail "no blank line after the disclaimer"
# free text preserved verbatim as prose.
printf '%s' "$c" | grep -qxF 'released: evil' || fail "free text 'released: evil' not preserved"
printf '%s' "$c" | grep -qxF 'claimed-by: evil' || fail "free text 'claimed-by: evil' not preserved"
# exactly one hidden marker carries the machine payload.
assert_marker 9 '{"type":"delivered","worker":"w1"}'
markers="$(printf '%s\n' "$c" | grep -c -- '<!-- kraken ')"
assert_eq "$markers" "1" "exactly one kraken marker in the produced comment"

# --- 4. the colliding free text still resets nothing it should not ----------
# The delivered marker (a real reset) opens the window; a challenger wins it.
# This proves the machine reset came from the MARKER, not the `released: evil`
# prose line — arbitration decodes only the marker.
grep -vxF "awaiting-merge" "$STATE/issues/9/labels" > "$STATE/issues/9/labels.tmp"
mv "$STATE/issues/9/labels.tmp" "$STATE/issues/9/labels"
out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 9 w2)"
assert_rc $? 0 "the delivered MARKER reset the window (not the prose)"
assert_eq "$out" "claim: claimed issue=9 worker=w2" "w2 wins the marker-reset window"

exit 0
