#!/usr/bin/env bash
# Conformance suite for the bundled transition program (kraken.py) — proves the
# queue protocol mechanically, no LLM involved. Each tests/t/*.sh runs in its own
# process against a fresh gh-stub state; the stub directory is prepended to
# PATH so the program under test calls the stub, never the real gh. Tests invoke
# `python3 skills/unleash/kraken.py <subcommand>` directly, so the shipped
# Python implementation is what the suite actually exercises.
#
# Requires `jq` (the stub evaluates kraken.py's own --jq expressions with it)
# and `python3` (kraken.py). Absent either = skip with exit 0, so the suite is
# safe on minimal machines; CI always has both. The kraken.py unit tests
# (tests/unit) run last — they cover the arbitration/parsing/pagination logic in
# isolation, with no gh at all.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ROOT
export PATH="$ROOT/tests/gh-stub:$PATH"

command -v jq >/dev/null 2>&1 || { echo "conformance: SKIP (jq not found)"; exit 0; }
command -v python3 >/dev/null 2>&1 || { echo "conformance: SKIP (python3 not found)"; exit 0; }

pass=0
fail=0
for t in "$ROOT"/tests/t/*.sh; do
  name="$(basename "$t")"
  if out="$(bash "$t" 2>&1)"; then
    printf 'ok    %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'FAIL  %s\n' "$name"
    printf '%s\n' "$out" | sed 's/^/      /'
    fail=$((fail + 1))
  fi
done

# Unit tests (every tests/unit/test_*.py) — kraken.py arbitration, machine-line
# parsing, comment pagination past 100, and the workflow commands, all with a
# mocked transport (no gh, no stub).
if [ -d "$ROOT/tests/unit" ]; then
  name="unit (tests/unit/test_*.py)"
  # discover exits 0 even when it collects zero tests, which would silently pass
  # this step if the pattern ever stops matching — guard against that false green
  # by asserting unittest reported at least one test ("Ran N tests").
  if out="$(cd "$ROOT" && python3 -m unittest discover -s tests/unit -p 'test_*.py' 2>&1)" \
     && ! printf '%s\n' "$out" | grep -Eq '^Ran 0 tests'; then
    printf 'ok    %s\n' "$name"
    pass=$((pass + 1))
  else
    printf 'FAIL  %s\n' "$name"
    printf '%s\n' "$out" | sed 's/^/      /'
    fail=$((fail + 1))
  fi
fi

echo
echo "conformance: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
