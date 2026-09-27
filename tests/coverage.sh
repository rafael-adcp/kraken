#!/usr/bin/env bash
# coverage.sh — line coverage of the transition program (skills/unleash/) across
# every token-free suite: unit, conformance and e2e (`make coverage`).
#
# Most of kraken.py runs as a SUBPROCESS — spawned by the conformance harness,
# by the hooks, and by the real agent CLIs' shell tools in the e2e suite — so a
# plain `coverage run` would see almost nothing. A sitecustomize.py on
# PYTHONPATH starts coverage in every Python process the suites spawn
# (COVERAGE_PROCESS_START), and the per-process data files are combined at the
# end. Needs the `coverage` package importable by `python3`; without it, and
# with `uv` on PATH, a throwaway venv is built for the run.
#
# A measurement, not a gate: it exits non-zero only when a suite fails, never on
# a percentage. Shell scripts (scripts/, hooks/) are not measured — their tests
# are the conformance and e2e cases that drive them.
#
#   make coverage              # report + HTML in htmlcov/ (COVERAGE_HTML=DIR to move it)
#   bash tests/coverage.sh     # report only; COVERAGE_HTML=DIR also writes HTML to DIR
set -u

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
# Anchor a relative report dir to the caller's cwd: we cd into $WORK below.
case "${COVERAGE_HTML:-}" in ''|/*) ;; *) COVERAGE_HTML="$PWD/$COVERAGE_HTML" ;; esac

if ! python3 -c 'import coverage' 2>/dev/null; then
  if command -v uv >/dev/null 2>&1; then
    echo "coverage: the coverage package is not installed — building a throwaway venv with uv"
    uv venv -q --python "$(command -v python3)" "$WORK/venv" \
      && uv pip install -q --python "$WORK/venv/bin/python" coverage \
      || { echo "coverage: could not build the venv" >&2; exit 2; }
    # First on PATH, so every `python3` the suites spawn can import coverage.
    export PATH="$WORK/venv/bin:$PATH"
  else
    echo "coverage: needs the coverage package (pip install coverage) or uv on PATH" >&2
    exit 2
  fi
fi

mkdir -p "$WORK/site" "$WORK/data"
printf 'import coverage\ncoverage.process_startup()\n' > "$WORK/site/sitecustomize.py"
cat > "$WORK/coveragerc" <<EOF
[run]
parallel = True
source = $ROOT/skills/unleash
data_file = $WORK/data/.coverage
[report]
skip_covered = True
show_missing = True
EOF
export COVERAGE_PROCESS_START="$WORK/coveragerc"
export PYTHONPATH="$WORK/site${PYTHONPATH:+:$PYTHONPATH}"

rc=0
for suite in unit conformance e2e; do
  echo "coverage: running tests/$suite"
  python3 -m unittest discover -s "$ROOT/tests/$suite" -p 'test_*.py' 2>&1 | tail -n 3 \
    | sed 's/^/  /'
  [ "${PIPESTATUS[0]}" -eq 0 ] || rc=1
done

cd "$WORK" || exit 2
unset COVERAGE_PROCESS_START
python3 -m coverage combine -q --rcfile="$WORK/coveragerc" "$WORK/data"
echo
python3 -m coverage report --rcfile="$WORK/coveragerc" | sed "s|$ROOT/||"
if [ -n "${COVERAGE_HTML:-}" ]; then
  python3 -m coverage html -q --rcfile="$WORK/coveragerc" -d "$COVERAGE_HTML" \
    && echo "coverage: HTML report in $COVERAGE_HTML"
fi
[ "$rc" -eq 0 ] || echo "coverage: a suite FAILED — the numbers above are from a failing run" >&2
exit "$rc"
