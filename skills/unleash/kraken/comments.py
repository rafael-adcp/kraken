"""The comment wire format: hidden markers, the attribution disclaimer, and
the task trailer. What a worker writes and what a reader parses back.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

import json
import re

from .contract import Issue, Json, Repo, Worker, plugin_version

# --- structured hidden markers -----------------------------------------------
# A state-changing comment (or a claim ref's commit message) carries its machine
# payload in ONE hidden HTML-comment marker, e.g.
#     <!-- kraken {"type":"claim","worker":"env-1"} -->
# Compact ASCII-only JSON: json.dumps avoids the CRLF/quoting/prefix hazards of
# the old visible-line grammar, and ASCII keeps the reaper/requeue greps
# locale-independent. Under protocol/4 markers are audit trail only — the CAS on
# the claim ref arbitrates, never a marker.
MARKER_PREFIX = "<!-- kraken "
MARKER_SUFFIX = " -->"
MARKER_RE = re.compile(r"<!--\s*kraken\s+(\{.*?\})\s*-->")

# Every marker "type" this program emits — the protocol/4 vocabulary. `claim`
# and `heartbeat` ride the claim ref's commit message; the rest head comments.
# `note` heads a free-form worker comment and changes no machine state — it
# exists only so the comment is recognizable as worker-authored (§4).
# `lease-expired` heads the steal's audit comment and is also COUNTED (§6's
# repeat-expiry guard), which is why it is a type of its own rather than a
# `stale-claim` reason. (`requeue` is operator-only.) The lint checks each
# against PROTOCOL.md's marker table via `kraken.py contract marker-types`.
MARKER_TYPES = ("claim", "heartbeat", "needs-decision", "delivered",
                "released", "stale-claim", "lease-expired", "note")


def make_marker(payload: Json) -> str:
    """Render a machine payload dict as the hidden marker (compact, ASCII-only)."""
    return MARKER_PREFIX + json.dumps(payload, separators=(",", ":")) + MARKER_SUFFIX


def parse_marker(line: str) -> Json | None:
    """Decode the kraken marker payload on a line, or None if it carries none.
    A body that is not a dict with a string "type" is treated as absent."""
    m = MARKER_RE.search(line)
    if not m:
        return None
    try:
        obj = json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        return None
    if isinstance(obj, dict) and isinstance(obj.get("type"), str):
        return obj
    return None


# The attribution disclaimer — the ONE authoritative definition of its format.
# Every worker authenticates as the operator, so without this blockquote a worker
# comment reads like a human's. It heads every transition comment (a blank line
# must follow, or GitHub folds the body into the quote). Deliberately
# agent-agnostic ("a kraken tentacle", not "a Claude Code tentacle"); every other
# consumer derives from this constant via `kraken.py contract`.
DISCLAIMER = "> 🐙 **Kraken worker `{worker}`** — automated comment from a kraken tentacle, not a human."


def disclaimer(worker: Worker) -> str:
    return DISCLAIMER.format(worker=worker)


# The Kraken-Task commit trailer — the ONE authoritative definition of its format,
# the delivery-side twin of DISCLAIMER: it maps a merged commit back to the task
# and the plugin version. `{version}` comes from plugin_version(), never a pasted
# literal. The companion `Co-Authored-By` line stays the agent's own.
TASK_TRAILER = "Kraken-Task: {repo}#{issue} (worker: {worker}, kraken@{version})"


def task_trailer(repo: Repo, issue: Issue, worker: Worker) -> str:
    """Compose the authoritative `Kraken-Task:` commit trailer, stamping the live
    plugin version so `kraken@<version>` is never guessed."""
    return TASK_TRAILER.format(
        repo=repo, issue=issue, worker=worker, version=plugin_version()
    )


def compose_comment(worker: Worker, prose: str, payload: Json) -> str:
    """Assemble a state-changing comment: disclaimer, human-facing prose, then the
    one hidden marker, blank-line separated so GitHub keeps them distinct."""
    parts = [disclaimer(worker)]
    prose = (prose or "").strip("\n")
    if prose:
        parts.append(prose)
    parts.append(make_marker(payload))
    return "\n\n".join(parts)


def compose_note(worker: Worker, prose: str) -> str:
    """A free-form worker comment (assumptions, progress prose): the attribution
    disclaimer, the prose, then a single non-state-changing `note` marker
    (PROTOCOL.md §4), blank-line separated so GitHub keeps them distinct. The
    marker is what makes the §6 requeue derivation read this as a worker comment
    *structurally* — the disclaimer stays as human-facing attribution but is no
    longer the arbiter. A `note` marker carries no machine state: the reconcile,
    the requeue derivation, and validate all treat it as inert (they key on their
    own marker types)."""
    return compose_comment(worker, prose, {"type": "note", "worker": worker})
