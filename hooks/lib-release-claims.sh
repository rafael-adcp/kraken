# lib-release-claims.sh — the shared claim-release loop behind the lifecycle
# hooks (session-end-release.sh, stop-failure-release.sh). Source it, then call
# `release_all_claims "<reason>"`. Not a hook itself.
#
# Discovery: `kraken.py claim` writes $KRAKEN_STATE_DIR/claim-<worker>.json on a
# won claim; deliver/escalate/release remove it. release_all_claims runs
# `kraken.py release` for every claim-*.json still present (in practice one; the
# glob also self-heals any straggler and needs no session->worker mapping).
#
# Best-effort: a failed release falls back to the reaper, never fails the caller.

# CLAUDE_PLUGIN_ROOT is set when Claude Code runs a hook; the dirname fallback
# keeps the scripts runnable standalone (tests).
KRAKEN_HOOKS_ROOT="${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"}"
KRAKEN_BIN="$KRAKEN_HOOKS_ROOT/skills/unleash/kraken.py"
KRAKEN_STATE="${KRAKEN_STATE_DIR:-$HOME/.kraken}"

# Read a top-level string field out of a claim JSON — jq if present, else a
# portable grep/sed fallback (the conformance suite keeps jq optional).
kraken_json_field() { # $1 = file, $2 = field
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$2" '.[$k] // empty' "$1" 2>/dev/null
  else
    sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -1
  fi
}

# release_all_claims REASON — run `kraken.py release` for every open claim on
# this machine. The released marker lands before in-progress drops and the claim
# ref last — the ordering that frees the lock honestly (PROTOCOL.md §9).
release_all_claims() {
  local reason="$1" f repo issue worker
  [ -d "$KRAKEN_STATE" ] || return 0
  shopt -s nullglob 2>/dev/null || true
  for f in "$KRAKEN_STATE"/claim-*.json; do
    [ -f "$f" ] || continue
    repo="$(kraken_json_field "$f" repo)"
    issue="$(kraken_json_field "$f" issue)"
    worker="$(kraken_json_field "$f" worker)"
    # Skip a malformed/empty file we cannot act on, never guess.
    [ -n "$repo" ] && [ -n "$issue" ] && [ -n "$worker" ] || continue
    python3 "$KRAKEN_BIN" release "$repo" "$issue" "$worker" "$reason" >/dev/null 2>&1 || true
  done
  return 0
}
