# lib-release-claims.sh — the shared claim-release loop behind the lifecycle
# hooks (session-end-release.sh, stop-failure-release.sh) and the Copilot
# ambush loop (scripts/kraken-loop.sh). Source it, then call
# `release_all_claims "<reason>" [worker]` or
# `release_session_claims "<reason>" <session-id>`. Not a hook itself.
#
# Discovery: `kraken.py claim` writes $KRAKEN_STATE_DIR/claim-<worker>.json on a
# won claim; deliver/escalate/release remove it. The release functions run
# `kraken.py release` for every claim-*.json still present that the caller may
# free: the Copilot loop scopes by its worker, SessionEnd by the Claude Code
# session the claim recorded (#173), StopFailure (account-wide) frees them all.
#
# Best-effort: a failed release falls back to the lease expiring on its own
# (PROTOCOL.md §5) — it never fails the caller. Everything here is an
# OPTIMIZATION over that expiry, not a precondition for recovery.

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

# release_claims_where REASON FIELD VALUE — run `kraken.py release` for every
# open claim on this machine, or, with FIELD, only the claims whose recorded
# FIELD equals VALUE. An empty VALUE for a named FIELD matches nothing: an owner
# we cannot name is an owner we cannot prove, and the lease TTL covers it.
# The released marker lands before in-progress drops and the claim ref last —
# the ordering that frees the lock honestly (PROTOCOL.md §9).
release_claims_where() {
  local reason="$1" field="${2:-}" value="${3:-}" f repo issue worker
  [ -z "$field" ] || [ -n "$value" ] || return 0
  [ -d "$KRAKEN_STATE" ] || return 0
  shopt -s nullglob 2>/dev/null || true
  for f in "$KRAKEN_STATE"/claim-*.json; do
    [ -f "$f" ] || continue
    repo="$(kraken_json_field "$f" repo)"
    issue="$(kraken_json_field "$f" issue)"
    worker="$(kraken_json_field "$f" worker)"
    # Skip a malformed/empty file we cannot act on, never guess.
    [ -n "$repo" ] && [ -n "$issue" ] && [ -n "$worker" ] || continue
    [ -z "$field" ] || [ "$(kraken_json_field "$f" "$field")" = "$value" ] || continue
    python3 "$KRAKEN_BIN" release "$repo" "$issue" "$worker" "$reason" >/dev/null 2>&1 || true
  done
  return 0
}

# release_all_claims REASON [WORKER] — every open claim on this machine, or,
# with WORKER, only that worker's own claim (a loop that knows its identity must
# never free a co-located worker's live claim).
release_all_claims() {
  if [ -n "${2:-}" ]; then
    release_claims_where "$1" worker "$2"
  else
    release_claims_where "$1"
  fi
}

# release_session_claims REASON SESSION_ID — only the claims made inside that
# Claude Code session (`kraken.py claim` records CLAUDE_CODE_SESSION_ID as
# "session"). SessionEnd fires for every session on the host, so this is the
# only scope that cannot free another session's live claim (#173).
release_session_claims() {
  release_claims_where "$1" session "${2:-}"
}

# hook_event_session_id — the `session_id` of the hook event JSON on stdin, or
# empty when stdin is not a JSON object naming one.
hook_event_session_id() {
  python3 -c '
import json, sys
try:
    event = json.load(sys.stdin)
except ValueError:
    event = None
sid = event.get("session_id") if isinstance(event, dict) else None
print(sid if isinstance(sid, str) else "")
' 2>/dev/null
}
