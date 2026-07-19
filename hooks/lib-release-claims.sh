# lib-release-claims.sh — the shared claim-release loop behind the bundled
# lifecycle hooks (session-end-release.sh, stop-failure-release.sh). Source it,
# then call `release_all_claims "<reason>"`. Not a hook itself — hooks.json
# never references this file.
#
# Discovery: `kraken.py claim` writes ${KRAKEN_STATE_DIR:-$HOME/.kraken}/claim-<worker>.json
# on a won claim; deliver/escalate/release remove it. release_all_claims runs
# `kraken.py release` for every claim-*.json still present in that dir. In
# practice there is exactly one (a worker holds at most one claim), but scanning
# the glob also self-heals any straggler and needs no session->worker mapping.
#
# Best-effort by contract: a failed release just falls back to the reaper — it
# never fails the caller. Callers still `exit 0` themselves.

# Locate the bundled program. CLAUDE_PLUGIN_ROOT is set when Claude Code runs
# a hook; the dirname fallback keeps the hook scripts runnable standalone
# (tests). BASH_SOURCE resolves this lib's own folder, which is the same
# hooks/ folder the callers live in.
KRAKEN_HOOKS_ROOT="${CLAUDE_PLUGIN_ROOT:-"$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"}"
KRAKEN_BIN="$KRAKEN_HOOKS_ROOT/skills/unleash/kraken.py"
KRAKEN_STATE="${KRAKEN_STATE_DIR:-$HOME/.kraken}"

# Read a top-level string field out of a claim JSON, jq if present else a
# portable grep/sed fallback (the conformance suite keeps jq optional).
kraken_json_field() { # $1 = file, $2 = field
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg k "$2" '.[$k] // empty' "$1" 2>/dev/null
  else
    sed -n 's/.*"'"$2"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$1" | head -1
  fi
}

# release_all_claims REASON — run `kraken.py release <claim> "REASON"` for every
# open claim on this machine. The released marker lands before in-progress
# drops, and the claim ref last — the ordering that frees the lock honestly
# (PROTOCOL.md §9). With no state dir or no claim files it is a strict no-op.
release_all_claims() {
  local reason="$1" f repo issue worker
  [ -d "$KRAKEN_STATE" ] || return 0
  shopt -s nullglob 2>/dev/null || true
  for f in "$KRAKEN_STATE"/claim-*.json; do
    [ -f "$f" ] || continue
    repo="$(kraken_json_field "$f" repo)"
    issue="$(kraken_json_field "$f" issue)"
    worker="$(kraken_json_field "$f" worker)"
    # A malformed/empty file we cannot act on: skip it, never guess.
    [ -n "$repo" ] && [ -n "$issue" ] && [ -n "$worker" ] || continue
    python3 "$KRAKEN_BIN" release "$repo" "$issue" "$worker" "$reason" >/dev/null 2>&1 || true
  done
  return 0
}
