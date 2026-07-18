#!/usr/bin/env python3
"""Token-free test runner for the kraken mechanics — the Python port of the old
`tests/run-tests.sh`. It runs two Python `unittest` suites and streams each
test file's output so `make check` and the CI log both show the literal
`python3 tests/<suite>/<file>` invocation and its `Ran N tests` summary:

  * tests/conformance/  — drives the bundled transition program (kraken.py)
    against the stateful Python `gh` stub (tests/gh-stub/gh): claim guard, claim
    race, claim-window arbitration, honest release, failure surfacing, reaper,
    requeue, validate, cleanup, init, status. The stub still evaluates
    kraken.py's own `--jq` expressions with the *real* `jq`, so the filters
    under test are the shipped ones.
  * tests/unit/         — kraken.py arbitration / marker parsing / comment
    pagination and the vendored workflow commands, with a mocked transport (no
    gh, no stub).

`jq` is required by the conformance stub; when it is absent the conformance
suite is skipped cleanly (the unit suite, which needs neither gh nor jq, still
runs) so the runner is safe on minimal machines. CI always has jq.

Exit status is non-zero iff any test file failed.
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))


def collect(rel_dir):
    d = os.path.join(ROOT, rel_dir)
    if not os.path.isdir(d):
        return []
    return [os.path.join(rel_dir, f) for f in sorted(os.listdir(d))
            if f.startswith("test_") and f.endswith(".py")]


def run_file(rel_path):
    print("\n--- python3 %s ---" % rel_path, flush=True)
    proc = subprocess.run([sys.executable, os.path.join(ROOT, rel_path)], cwd=ROOT)
    ok = proc.returncode == 0
    print(("ok    " if ok else "FAIL  ") + rel_path, flush=True)
    return ok


def main():
    have_jq = shutil.which("jq") is not None
    files = list(collect("tests/unit"))
    if have_jq:
        files = list(collect("tests/conformance")) + files
    else:
        print("conformance: SKIP (jq not found) — running unit suite only", flush=True)

    if not files:
        print("no test files found", file=sys.stderr)
        return 1

    passed = failed = 0
    for rel in files:
        if run_file(rel):
            passed += 1
        else:
            failed += 1

    print("\ntests: %d passed, %d failed" % (passed, failed), flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
