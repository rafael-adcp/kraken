#!/usr/bin/env bash
# kraken-loop.sh — external "ambush" loop for a tentacle in any harness without a
# native background watcher: GitHub Copilot CLI (the default), Pi, or another
# agent CLI.
#
# Such a harness has no Monitor tool to arm kraken's zero-token watcher (SKILL.md,
# "Staying in ambush"), so this is the documented fallback: an outside-the-model shell loop
# running ONE `--once`-style drain per iteration (a fresh agent process per
# pass also gives each task the fresh-context isolation a subagent would).
#
# Two directories, kept apart: the WORK DIR is where the agent runs (the work
# repo's checkout — the code, the branch and the draft PR live there), and the
# kraken checkout this script sits in is where the operating contract lives
# (AGENTS.md, SKILL.md, PROTOCOL.md, kraken.py). The only AGENTS.md the agent
# auto-loads is the work repo's own, so copilot's prompt names the contract
# files by absolute path and `--add-dir` grants it read access to the checkout;
# Pi loads the skills from the checkout with `--skill`.
#
# Cost control: each poll first runs the FREE, read-only `kraken.py
# list-startable`; the model is only invoked when a task is actually startable,
# so an idle queue never spends a token. The operator owns cadence + stop
# (Ctrl-C); it never outlives this terminal. Run it straight from a checkout.
#
# Self-heal: under kraken-protocol/9 a claim is a LEASE that expires on its own, so an
# abandoned task comes back within one TTL no matter how the drain died. This
# loop just makes it immediate: if a drain dies holding a lease (agent crash,
# rate-limit abort, Ctrl-C), it releases on the spot via
# hooks/lib-release-claims.sh instead of waiting out the TTL.
#
# Usage:
#   scripts/kraken-loop.sh OWNER/tasks --worker-name <name> --project <name> \
#                          [--harness copilot|pi] [--work-dir <dir>] \
#                          [--poll <seconds>] [--once]
#
# Every flag also has an env-var fallback (KRAKEN_TASKS, KRAKEN_WORKER,
# KRAKEN_PROJECT, KRAKEN_HARNESS, KRAKEN_WORK_DIR, KRAKEN_POLL_SECONDS); an
# explicit flag wins over the env var. The work dir defaults to the directory
# you run it from. KRAKEN_AGENT_CMD runs an unlisted agent CLI instead.
set -u

# --- locate the kraken checkout ---------------------------------------------
# REPO_DIR derives from this script's own location, so the loop works from any
# checkout without a hardcoded path. It is where the contract lives, never where
# the work happens — that is WORK_DIR.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="${KRAKEN_REPO_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
KRAKEN_PY="$REPO_DIR/skills/unleash/kraken.py"

usage() {
  cat <<'EOF'
Usage:
  scripts/kraken-loop.sh OWNER/tasks --worker-name <name> --project <name> \
                         [--harness copilot|pi] [--work-dir <dir>] \
                         [--poll <seconds>] [--once]

Continuous "ambush" loop for a tentacle in any harness without a native
background watcher: each poll runs the free, read-only `kraken.py
list-startable` and only invokes the model when the queue has a startable task.
Ctrl-C to stop; it never outlives this terminal.

Flags (each also has an env-var fallback; an explicit flag wins):
  --worker-name <name>   worker identity in every claim/comment   [KRAKEN_WORKER]
  --project <name>       only take project:<name> tasks           [KRAKEN_PROJECT]
  --harness <name>       agent CLI: copilot (default) or pi       [KRAKEN_HARNESS]
  --work-dir <dir>       the work repo checkout the agent runs in
                         (default: the current directory)        [KRAKEN_WORK_DIR]
  --poll <seconds>       poll cadence (default 60)          [KRAKEN_POLL_SECONDS]
  --once                 drain once and exit (no polling loop)
  -h, --help             show this help

The OWNER/tasks coordination-repo slug is positional [KRAKEN_TASKS].

KRAKEN_AGENT_CMD runs an unlisted agent CLI instead of --harness: it is split
on whitespace (no quoting, no globbing, no eval) and the drain prompt is
appended as its last argument, e.g. KRAKEN_AGENT_CMD="mycli --print --yes".
An argument containing spaces needs a wrapper script.
EOF
}

# --- config: flags override env vars --------------------------------------
TASKS="${KRAKEN_TASKS:-}"
WORKER="${KRAKEN_WORKER:-}"
PROJECT="${KRAKEN_PROJECT:-}"
HARNESS="${KRAKEN_HARNESS:-copilot}"
WORK_DIR="${KRAKEN_WORK_DIR:-$PWD}"
POLL="${KRAKEN_POLL_SECONDS:-60}"
ONCE=0

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help)         usage; exit 0 ;;
    --worker-name)     WORKER="${2:-}"; shift 2 ;;
    --project)         PROJECT="${2:-}"; shift 2 ;;
    --harness)         HARNESS="${2:-}"; shift 2 ;;
    --work-dir)        WORK_DIR="${2:-}"; shift 2 ;;
    --poll)            POLL="${2:-}"; shift 2 ;;
    --once)            ONCE=1; shift ;;
    -*)                echo "kraken-loop: unknown flag: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [ -z "$TASKS" ]; then TASKS="$1"; shift
      else echo "kraken-loop: unexpected argument: $1" >&2; usage >&2; exit 2; fi
      ;;
  esac
done

missing=""
[ -n "$TASKS" ]   || missing="$missing OWNER/tasks"
[ -n "$WORKER" ]  || missing="$missing --worker-name"
[ -n "$PROJECT" ] || missing="$missing --project"
if [ -n "$missing" ]; then
  echo "kraken-loop: missing required argument(s):$missing" >&2
  usage >&2
  exit 2
fi

case "$TASKS" in
  OWNER/*|*'<'*|*'>'*)
    echo "kraken-loop: '$TASKS' looks like the template placeholder — pass your real owner/repo." >&2
    exit 2 ;;
esac

# Refuse a bad harness before anything has a side effect (the cd, the EXIT
# trap). KRAKEN_AGENT_CMD, when set, replaces the harness table entirely.
if [ -n "${KRAKEN_AGENT_CMD:-}" ]; then
  read -r -a AGENT_BASE <<<"$KRAKEN_AGENT_CMD"
  [ "${#AGENT_BASE[@]}" -gt 0 ] || { echo "kraken-loop: KRAKEN_AGENT_CMD names no command." >&2; exit 2; }
else
  case "$HARNESS" in
    copilot|pi) ;;
    *)
      echo "kraken-loop: unknown harness '$HARNESS' (expected copilot or pi; set KRAKEN_AGENT_CMD for another CLI)." >&2
      usage >&2
      exit 2 ;;
  esac
fi

[ -f "$KRAKEN_PY" ] || { echo "kraken-loop: cannot find $KRAKEN_PY — run from a kraken checkout." >&2; exit 1; }
[ -d "$WORK_DIR" ] || { echo "kraken-loop: --work-dir '$WORK_DIR' is not a directory." >&2; exit 2; }
# Absolute, so the prompt never names a path relative to a cwd the agent may leave.
WORK_DIR="$(CDPATH= cd -- "$WORK_DIR" && pwd)" && cd "$WORK_DIR" \
  || { echo "kraken-loop: cannot cd into $WORK_DIR" >&2; exit 1; }

# --- fast claim-release (self-heal) ----------------------------------------
# The SessionEnd/StopFailure hooks only fire inside a plugin-loading session
# (Claude Code, Copilot CLI), and this loop runs none, so it is the equivalent
# for every harness it drives. `kraken.py claim` writes
# $KRAKEN_STATE_DIR/claim-<worker>.json and every terminal transition
# (deliver/escalate/release) removes it, so a file that survives the agent
# process is proof the drain died holding the lease (crash, kill, rate-limit
# abort). Release it on the spot — scoped to THIS worker's lease only — so the
# task requeues in seconds instead of at the lease TTL (PROTOCOL.md §5).
# Best-effort: a failed release falls back to that expiry, which is the actual
# recovery mechanism; this is only the fast path. `release` itself refuses (exit
# 10) if the lease has already been stolen, so a late release can never free a
# task another worker is running.
. "$REPO_DIR/hooks/lib-release-claims.sh"

release_own_claim() { # $1 = reason
  release_all_claims "$1" "$WORKER"
}

# The trap covers the loop itself dying while a drain is in flight: Ctrl-C
# (SIGINT hits the whole foreground group, killing the agent first), kill, or a
# closing terminal. Idempotent — a released claim leaves no state file behind.
trap 'release_own_claim "kraken-loop terminated"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# The prompt and permission flags are single-sourced with the agent-behavior
# harness (tests/agent/), so what it judges is exactly what this loop runs.
# The auth precheck is kraken.py's own GitHub token for the queue reads, so it
# guards every harness. The agent's model login gets no precheck: `pi auth
# check` needs a --provider/--model this loop does not know, and a missing
# login only fails the drain, which the release below already covers.
. "$REPO_DIR/scripts/lib-copilot-drain.sh"
kraken_require_github_auth kraken-loop || exit 1
PROMPT="$(kraken_copilot_prompt "$TASKS" "$PROJECT" "$WORKER" "$WORK_DIR" "$REPO_DIR")"

# --- the agent command: one argv per harness -------------------------------
# A bash array run as "${AGENT_CMD[@]}", never eval'd, so a slug, worker or
# project name can never be re-parsed as shell. AGENT_NAME names the agent in
# the release reason ("copilot exited mid-drain"), so a thread says which
# harness walked away.
if [ -n "${KRAKEN_AGENT_CMD:-}" ]; then
  # Escape hatch: AGENT_BASE was split above with `read -a` (no quote
  # processing, no glob expansion, no eval); the prompt goes last, as one
  # argument.
  AGENT_CMD=("${AGENT_BASE[@]}" "$PROMPT")
  AGENT_NAME="$(basename -- "${AGENT_BASE[0]}")"
else
  AGENT_NAME="$HARNESS"
  case "$HARNESS" in
    # Add -s/--silent for terser logs.
    copilot) AGENT_CMD=(copilot -p "$PROMPT" --add-dir "$REPO_DIR" "${KRAKEN_COPILOT_FLAGS[@]}") ;;
    # Pi loads the skills from the kraken checkout (it has no --add-dir) and
    # runs the skill itself. No -a/--approve: print mode never blocks on
    # project trust, so the work repo's .pi/ loads only if the operator already
    # trusts it. The deny rules copilot gets from KRAKEN_COPILOT_FLAGS come in
    # two halves, as Pi has no command-pattern --deny-tool: --exclude-tools
    # refuses the GitHub MCP merge / issue-write tools whole (Pi names them
    # mcp__<server>__<tool>, so `*` catches any server name), and the kraken
    # extension loaded with -e refuses `gh pr merge`, `gh repo delete` and
    # `gh issue close` in the bash tool (extensions/pi/deny.py).
    pi) AGENT_CMD=(pi -p --skill "$REPO_DIR/skills" -e "$REPO_DIR/extensions/pi/kraken.ts"
                   --exclude-tools 'mcp__*__merge_pull_request,mcp__*__issue_write'
                   -- "/skill:unleash $TASKS --worker-name $WORKER --project $PROJECT --once") ;;
  esac
fi

drain_pass() {
  local ts startable rc
  ts="$(date -u +%H:%M:%SZ)"
  startable="$(python3 "$KRAKEN_PY" list-startable "$TASKS" "$PROJECT")"
  rc=$?
  if [ "$rc" -ne 0 ]; then
    # Never report a failed read as an idle queue: the two look identical from
    # here, and only one of them is fine to leave running overnight.
    echo "kraken-loop: $ts queue read failed (kraken.py exit $rc) — skipping model; check the token and the network." >&2
    return 1
  fi
  if [ -n "$startable" ]; then
    echo "kraken-loop: $ts startable task(s) — running a drain pass:"
    printf '%s\n' "$startable" | sed 's/^/  /'
    # The agent's exit status is deliberately ignored: the lease, not the exit
    # code, says whether the drain died. A clean deliver leaves nothing to
    # release; a failure exits non-zero without saying whether it got as far
    # as a claim (`pi -p` exits 1 alike for a bad --model, an auth failure and
    # a refused request), so a surviving lease is released below either way.
    # stdin is closed: a headless drain never reads it, and `pi -p` waits for
    # EOF on an open non-tty stdin (ssh without a tty, systemd, CI) forever.
    "${AGENT_CMD[@]}" </dev/null
    # A lease that survived the agent process is abandoned — nobody is left
    # to finish or renew it. Free the task now, not at the TTL.
    release_own_claim "$AGENT_NAME exited mid-drain"
    return 0
  fi
  echo "kraken-loop: $ts queue idle — skipping model."
  return 1
}

if [ "$ONCE" -eq 1 ]; then
  echo "kraken-loop: single drain pass over $TASKS project:$PROJECT as $WORKER in $WORK_DIR."
  drain_pass || true
  exit 0
fi

echo "kraken-loop: watching $TASKS project:$PROJECT as $WORKER in $WORK_DIR (poll ${POLL}s). Ctrl-C to stop."
while true; do
  drain_pass || true
  sleep "$POLL"
done
