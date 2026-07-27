#!/usr/bin/env python3
"""kraken.py — the executable entry point for the coordination protocol.

The implementation is the `kraken/` package next to this file, one module per
layer; its `__init__.py` documents the layering. This file stays because it is
the path every caller already names — the unleash and status skills, the
lifecycle hooks, the supervising `loop`, and the conformance harness all invoke
`python3 <skill>/kraken.py <subcommand>`. Nothing
outside had to change when the module became a package, and nothing outside
should have to know it did.

Exit-code contract (PROTOCOL.md §12), preserved verbatim from the scripts:
    0   success
    10  lost the claim CAS — another worker holds the claim ref; back off
        (writing nothing) and pick the next candidate
    11  no longer clear — a held label appeared since listing; skip the task
    20  gh / network transport failure — state unknown, re-check before retry
    3   claim-next only: no candidate was startable — the queue is empty, or
        every candidate turned out held/lost as it iterated (nothing to claim,
        an honest empty result, not a fault)
    2   bad invocation (missing file / unknown mode)
"""
import os
import sys

# Run from anywhere: as `__main__` this file's directory is not necessarily on
# the path, and the package sits beside it. A directory package wins over a
# same-named module in the same path entry, so `import kraken` here resolves to
# the package rather than recursively to this file.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kraken.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
