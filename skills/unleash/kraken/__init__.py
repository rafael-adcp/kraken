"""kraken — the coordination protocol as one stdlib-only program.

A queue of GitHub Issues in a private coordination repo, drained by named
workers that claim one task at a time. The claim is a git ref, created with a
compare-and-swap that only one racer can win, carrying a LEASE: the ref's
server-stamped commit date, which a reader past the TTL treats as expired and
steals. Labels are projection — written so a human scanning the issue list can
see what the refs cannot show them, and read by nothing (PROTOCOL.md §3).

The package is layered, leaf first; every arrow points down and the graph is
acyclic:

    contract     exit codes, wire types, diagnostics — depends on nothing
    comments     the comment wire format: markers, disclaimer, trailer
    transport    Api: the one object that talks to GitHub
    lease        the Lease object, the TTL, the local claim-state file
    refs         the claim-ref CAS — the generation ladder
    queue        the batched walk, hydration, the startable filter
    render       report objects to text
    reconcile    the §6 repair pass
    claim        the contended claim sequence and the terminal transitions
    next_action  the driver loop as one envelope
    watch        the zero-token ambush
    status       the read-only console
    workflow     init, validate, cleanup
    cli          argument parsing and dispatch

`skills/unleash/kraken.py` next to this package is the executable entry point
and is what every caller — the skills, the hooks, the workflows, the conformance
harness — invokes. It imports `cli.main` and nothing else.

Exit codes are the contract between this program and whatever drives it:

    0   ok
    3   nothing to do (an empty queue is not a failure)
    10  lost — somebody else holds the task
    11  not clear — the task is held by a label
    20  transport failure; state is UNKNOWN, re-check before retrying
    64  usage

Everything below re-exports the package's public surface, so `import kraken`
gives the same names the single-file module did.
"""
from __future__ import annotations

from .contract import (
    ClaimRecord, CommentRecord, CommitMeta, ENTRYPOINT, EXIT_LOST, EXIT_NONE,
    EXIT_NOT_CLEAR, EXIT_OK, EXIT_TRANSPORT, EXIT_UNKNOWN_PROJECT, EXIT_USAGE,
    Envelope, Epoch, Gen, HELD_LABELS, Issue, Json, LEGACY_CLAIM_GEN, Node,
    PLUGIN_MANIFEST, PLUGIN_VERSION_UNKNOWN, PRIORITY_LABEL, PROTOCOL_VERSION,
    ReconcileAction, Repo, SKILL_DIR, Sha, Worker, diag,
    diagnostics_on_stderr, plugin_version
)
from .comments import (
    DISCLAIMER, MARKER_PREFIX, MARKER_RE, MARKER_SUFFIX, MARKER_TYPES,
    TASK_TRAILER, compose_comment, compose_note, disclaimer, make_marker,
    parse_marker, task_trailer
)
from .transport import (
    Api, DEFAULT_API_URL, HTTP_TIMEOUT_SECONDS, PER_PAGE,
    STATUS_NETWORK_FAILURE, api_base, github_token, quote_path
)
from .lease import (
    LEASE_DEFAULT_TTL_SECONDS, LEASE_EXPIRY_ESCALATE, LEASE_RENEW_DIVISOR,
    Lease, NO_LEASE, UNREADABLE_LEASE, claim_state_path, clear_claim_state,
    format_age, format_iso, holder_shas, lease_renew_seconds, lease_state,
    lease_ttl_seconds, live_leases, open_claim, open_claim_record, parse_iso,
    refuse_second_claim, state_dir, wake_retry_flag_path, wake_retry_mtime,
    write_claim_state
)
from .refs import (
    CLAIM_REF_PREFIX, EMPTY_TREE_SHA, Refs, claim_ref, parse_claim_ref
)
from .queue import (
    COMMENT_HYDRATE_CHUNK, DEPENDS_ON_RE, NO_RESPONSE_PLACEHOLDER,
    QUEUE_COMMENT_WINDOW, Candidate, Queue, claim_meta_of, cmd_list_startable,
    comment_hungry, comment_nodes_of, depends_on_ref, has_requeue_directive,
    is_empty_section, is_worker_comment, label_names_of, node_state,
    project_names_of, requeue_verdict, requeued_labels, section_body
)
from .render import (
    render_init, render_next_action, render_status
)
from .reconcile import (
    RECONCILER_WORKER, apply_reconcile, cmd_reap, expiry_count,
    project_reconcile, reconcile_pass, reconcile_plan, stale_claim_body
)
from .claim import (
    ClaimAttempt, DELIVER, ESCALATE, RELEASE, Terminal, TerminalTransition,
    acquire_next, cmd_claim, cmd_claim_next, cmd_deliver, cmd_escalate,
    cmd_heartbeat, cmd_note, cmd_release, lease_expired_body, list_projects,
    probe_lease_state, read_body_file, verify_project
)
from .next_action import (
    NEXT_ACTIONS, NEXT_ACTION_EXIT, NextAction, NextActionEnvelope,
    cmd_next_action, issue_is_finished, lease_block, next_action,
    next_action_envelope, resume_verdict, task_brief, then_commands
)
from .watch import (
    WATCH_MAX_FAILURES, WATCH_WARN_EVERY, cmd_watch, snapshot_state,
    wake_retry_due, watch, watch_failure_action
)
from .status import (
    cmd_status, compute_status, parse_github_pr_url, parse_pr_url,
    pr_is_merged, queue_hygiene
)
from .workflow import (
    CANONICAL_LABELS, INIT_ASSETS, OBSOLETE_ASSETS, PROJECT_LABEL_COLOR,
    PROJECT_LABEL_DESC, VALIDATE_ACCEPTANCE_MISSING, VALIDATE_GOAL_MISSING,
    VALIDATE_PROJECT_MISSING, VALIDATION_MARKER, bundled_asset, cmd_cleanup,
    cmd_init, cmd_validate, is_identity_label, latest_validation_comment,
    validation_body
)
from .cli import (
    CONTRACT_FIELDS, build_parser, cmd_contract, main
)
