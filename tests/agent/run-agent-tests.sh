#!/usr/bin/env bash
# Agent-behavior harness: drives headless Claude Code against the gh-stub to test
# the SKILL's judgment (the contract MUSTs that live in the model's behavior),
# not just the transition program. Each scenarios/*.sh seeds one crafted queue +
# task body, runs a real `claude -p "/kraken:unleash ... --once"`, and asserts on
# ARTIFACTS (stub labels/machine lines + a real local work repo).
#
# REAL model runs — NOT part of the mechanical conformance suite. It runs from
# the pre-push hook when a push touches skills/ or tests/agent/, and by hand.
# Requires: claude on PATH, ANTHROPIC_API_KEY (or logged-in CLI), jq, git —
# absent any, SKIP with exit 0, never a false failure.
#
# Advisory scenarios (flaky by nature) are listed in ADVISORY: they run and
# report, but a failure does not fail the suite. Blocking scenarios must pass.
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
export ROOT
AGENT_DIR="$ROOT/tests/agent"

# --- preflight: skip cleanly when the harness cannot run for real -------------
skip() { echo "agent-conformance: SKIP ($1)"; exit 0; }

# Bail early on native Windows: the harness relies on the gh-stub shadowing the
# real `gh` through PATH, which is unreliable there (an extensionless stub does
# not reliably beat `gh.exe`), so scenarios fail for environment reasons. Run it
# under WSL or Linux instead.
case "$(uname -s 2>/dev/null)" in
  MINGW* | MSYS* | CYGWIN* | Windows_NT)
    echo "agent-conformance: SKIP (native Windows — the gh-stub cannot reliably shadow the real gh here)"
    echo "  Run this harness under WSL or Linux, where the stub wins on PATH:"
    echo "    wsl  # then, in the repo:  make test-agent"
    exit 0
    ;;
esac

command -v jq  >/dev/null 2>&1 || skip "jq not found"
command -v git >/dev/null 2>&1 || skip "git not found"
command -v claude >/dev/null 2>&1 || skip "claude CLI not on PATH"
if [ -z "${ANTHROPIC_API_KEY:-}" ] && [ "${KRAKEN_AGENT_ASSUME_AUTH:-0}" != "1" ]; then
  skip "ANTHROPIC_API_KEY unset (set KRAKEN_AGENT_ASSUME_AUTH=1 to run against a logged-in CLI)"
fi

# Scenarios whose pass/fail is advisory (reported, never blocking). Override with
# ADVISORY="..." in the environment; empty means every scenario blocks.
ADVISORY="${ADVISORY:-01-prompt-injection.sh}"
is_advisory() { case " $ADVISORY " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

# Optional filter: run only scenarios whose name matches $1 (e.g. "04").
FILTER="${1:-}"

pass=0 fail=0 advisory_fail=0 skipped=0
for t in "$AGENT_DIR"/scenarios/*.sh; do
  name="$(basename "$t")"
  if [ -n "$FILTER" ] && ! printf '%s' "$name" | grep -q -- "$FILTER"; then
    continue
  fi
  printf '.. %-28s ' "$name"
  out="$(bash "$t" 2>&1)"; rc=$?
  case "$rc" in
    0)
      echo "ok"
      pass=$((pass + 1))
      ;;
    2)  # the scenario cannot run for real in this environment (honest skip)
      echo "skip"
      skipped=$((skipped + 1))
      printf '%s\n' "$out" | grep '^SKIP:' | sed 's/^/     /'
      ;;
    *)
      if is_advisory "$name"; then
        echo "ADVISORY-FAIL"
        advisory_fail=$((advisory_fail + 1))
      else
        echo "FAIL"
        fail=$((fail + 1))
      fi
      printf '%s\n' "$out" | sed 's/^/     /'
      ;;
  esac
done

echo
echo "agent-conformance: $pass passed, $fail failed, $advisory_fail advisory-failed, $skipped skipped"
[ "$fail" -eq 0 ]
