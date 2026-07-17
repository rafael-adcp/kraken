#!/usr/bin/env bash
# protocol/1 -> /2 migration (PROTOCOL.md §4): a protocol/2 producer emits the
# hidden marker ONLY, while a protocol/2 consumer still arbitrates a pre-existing
# protocol/1 line-grammar thread — and the two formats interoperate within one
# thread. Driven end to end through the real kraken.py + gh-stub pipeline.
. "$ROOT/tests/lib.sh"

# --- 1. a legacy protocol/1 thread still arbitrates --------------------------
# Seeded with the retired visible line grammar only (what a pre-migration thread
# holds): a dead claim, a legacy `delivered:` reset (a review bounce), then a
# legacy `claimed-by:` heir. A protocol/2 challenger must lose to the heir the
# legacy grammar names — arbitration reads protocol/1 exactly as it always did.
mk_issue 7 "legacy protocol/1 thread" kraken-task "project:app"
mk_comment 7 "claimed-by: dead"
mk_comment 7 "delivered: dead"
mk_comment 7 "claimed-by: heir"

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 7 challenger)"
assert_rc $? 10 "challenger loses to the legacy-grammar heir"
assert_eq "$out" "claim: lost-tiebreaker issue=7 winner=heir" \
  "a protocol/1 thread arbitrates unchanged under a protocol/2 consumer"

# --- 2. a producer wins a reset legacy window and emits a MARKER, not a line -
# The legacy claim is closed by a legacy `released:` reset, so the window is
# open. `kraken.py claim` (a protocol/2 producer) wins it — and the comment it
# writes carries the hidden marker, never a protocol/1 `claimed-by:` line.
mk_issue 8 "reset legacy window, protocol/2 producer" kraken-task "project:app"
mk_comment 8 "claimed-by: gone"
mk_comment 8 "released: gone"

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 8 fresh)"
assert_rc $? 0 "protocol/2 producer wins the reset legacy window"
assert_marker 8 '{"type":"claim","worker":"fresh"}'
last_comment 8 | grep -q '^claimed-by:' \
  && fail "protocol/2 producer emitted a legacy claimed-by: line"

# --- 3. protocol/2 arbitration reads its own marker --------------------------
# A challenger on a thread whose only claim is a protocol/2 marker (no held
# label, so the guard passes) must lose — the winner is decoded back out of the
# marker, closing the produce-marker -> arbitrate-marker loop.
mk_issue 9 "protocol/2 marker thread" kraken-task "project:app"
mk_comment 9 '<!-- kraken {"type":"claim","worker":"owner2"} -->'

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 9 challenger)"
assert_rc $? 10 "challenger loses to a protocol/2 marker claim"
assert_eq "$out" "claim: lost-tiebreaker issue=9 winner=owner2" \
  "arbitration decodes a protocol/2 marker claim"

# --- 4. the two formats interoperate within one thread -----------------------
# A protocol/1 `claimed-by:` claim, cleared by a protocol/2 `stale-claim` marker
# reset, then a protocol/2 `claim` marker: the cross-format reset must clear the
# legacy claim so the newest marker claim wins.
mk_issue 10 "mixed protocol/1 + /2 thread" kraken-task "project:app"
mk_comment 10 "claimed-by: old"
mk_comment 10 '<!-- kraken {"type":"stale-claim","reason":"no activity for 7h"} -->'
mk_comment 10 '<!-- kraken {"type":"claim","worker":"new"} -->'

out="$(python3 "$SCRIPTS/kraken.py" claim OWNER/tasks 10 challenger)"
assert_rc $? 10 "challenger loses to the post-reset marker claim"
assert_eq "$out" "claim: lost-tiebreaker issue=10 winner=new" \
  "a protocol/2 reset clears a protocol/1 claim (cross-format arbitration)"

exit 0
