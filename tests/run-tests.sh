#!/usr/bin/env bash
# Conformance suite for the bundled transition scripts — proves the queue
# protocol mechanically, no LLM involved. Each tests/t/*.sh runs in its own
# process against a fresh gh-stub state; the stub directory is prepended to
# PATH so the scripts under test call the stub, never the real gh.
#
# Requires `jq` (the stub evaluates the scripts' own --jq expressions with it).
# Absent jq = skip with exit 0, so the suite is safe on minimal machines; CI
# always has it.
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export ROOT
export PATH="$ROOT/tests/gh-stub:$PATH"

command -v jq >/dev/null 2>&1 || { echo "conformance: SKIP (jq not found)"; exit 0; }

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

echo
echo "conformance: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
