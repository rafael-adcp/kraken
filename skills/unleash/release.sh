#!/usr/bin/env bash
# release.sh — thin shim. The transition logic now lives in kraken.py (one
# stdlib-only program with a subcommand per transition); this wrapper keeps the
# historical entry point working for the unleash skill, the conformance suite,
# and the SessionEnd hook. See kraken.py for the behavior and exit-code contract.
#
# usage: release.sh OWNER/tasks ISSUE WORKER_NAME [REASON]
exec python3 "$(dirname "$0")/kraken.py" release "$@"
