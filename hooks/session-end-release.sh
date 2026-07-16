#!/usr/bin/env bash
# session-end-release.sh — the bundled SessionEnd hook (registered in
# hooks/hooks.json).
#
# When a worker's Claude Code session ends *gracefully* (terminal closed,
# /exit) while it still holds an open claim, this releases that claim
# automatically: the task returns to the queue in seconds instead of waiting
# ~6h for the reaper. It runs `kraken.py release`, so `released: <worker>` lands
# before in-progress drops — the ordering that closes the claim window (§9).
#
# Scope, honestly: this covers a graceful end only. A Claude usage limit does
# NOT end the session (the turn aborts, the session stays open waiting for
# input), so SessionEnd never fires — same for a hard kill / crash / power loss.
# The reaper stays the backstop for those. See the #60 FAQ in README.md.
#
# Discovery: `kraken.py claim` writes ${KRAKEN_STATE_DIR:-$HOME/.kraken}/claim-<worker>.json
# on a won claim; deliver/escalate/release remove it. This hook releases every
# claim-*.json still present in that dir. In practice there is exactly one (a
# worker holds at most one claim), but scanning the glob also self-heals any
# straggler and needs no session->worker mapping.
#
# Best-effort by contract: this ALWAYS exits 0. A failed release just falls back
# to the reaper; session exit must never be blocked. Claude Code feeds a
# SessionEnd JSON event on stdin; we don't need it, so we ignore it.
set -u

# Locate the bundled program. CLAUDE_PLUGIN_ROOT is set when Claude Code runs
# the hook; the dirname fallback keeps the script runnable standalone (tests).
ROOT_DIR="${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "$0")/.." && pwd)"}"
KRAKEN="$ROOT_DIR/skills/unleash/kraken.py"
STATE_DIR="${KRAKEN_STATE_DIR:-$HOME/.kraken}"

# No state dir or no claim files -> strict no-op, no writes.
[ -d "$STATE_DIR" ] || exit 0

# Read a top-level string field out of a claim JSON, jq if present else a
# portable grep/sed fallback (the conformance suite keeps jq optional).
json_field() { # $1 = file, $2 = field
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$2" '.[$k] // empty' "$1" 2>/dev/null
  else
    sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -1
  fi
}

shopt -s nullglob 2>/dev/null || true
for f in "$STATE_DIR"/claim-*.json; do
  [ -f "$f" ] || continue
  repo="$(json_field "$f" repo)"
  issue="$(json_field "$f" issue)"
  worker="$(json_field "$f" worker)"
  # A malformed/empty file we cannot act on: skip it, never guess.
  [ -n "$repo" ] && [ -n "$issue" ] && [ -n "$worker" ] || continue

  # Best-effort: kraken.py release posts released: then drops in-progress and
  # removes the state file. If it fails (network down), the reaper still backs us
  # up — do not let it fail the hook or block the session from exiting.
  python3 "$KRAKEN" release "$repo" "$issue" "$worker" "session ended" >/dev/null 2>&1 || true
done

exit 0
