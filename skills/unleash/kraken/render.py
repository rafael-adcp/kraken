"""Report objects to human-readable text. No I/O of its own.

Part of the kraken protocol package; see __init__.py."""
from __future__ import annotations

from .contract import Envelope, Json
from .lease import format_age

def render_next_action(env: Envelope) -> None:
    """The --text rendering: the same verdict as one line a human can scan,
    plus what the agent owes next. The JSON stays the machine contract."""
    action = env["action"]
    head = f"next-action: {action}"
    if "issue" in env:
        head += f" issue={env['issue']}"
    head += f" repo={env['repo']} worker={env['worker']}"
    if env.get("holding"):
        head += f" holding={env['holding']['repo']}#{env['holding']['issue']}"
    if env.get("resumed") is not None:
        head += f" resumed={str(env['resumed']).lower()}"
    print(head)
    if env.get("detail"):
        print(f"  {env['detail']}")
    brief = env.get("brief")
    if brief:
        print(f"  title: {brief['title']}")
        for field in ("goal", "acceptance"):
            if brief[field]:
                first = brief[field].split("\n")[0]
                print(f"  {field}: {first}")
    lease = env.get("lease")
    if lease:
        print(f"  lease: renew every {lease['renew_every_seconds']}s; "
              f"expires {lease['expires_at']} "
              f"({lease['seconds_remaining']}s left"
              f"{', RENEW NOW' if lease['renew_now'] else ''})")
    for name, command in (env.get("then") or {}).items():
        print(f"  {name}: {command}")


def render_status(report: Json) -> str:
    """The human console — the shape skills/status/SKILL.md documents. A thin
    renderer over StatusReport's report; empty groups say so plainly."""
    repo = report["repo"]
    project = report["project"]
    scope = f"project:{project} @ {repo}" if project else f"@ {repo}"
    lines = [f"🐙 kraken status — {scope}", ""]

    review = report["review_queue"]
    lines.append(f"  📋 Review queue (awaiting-merge) — "
                 f"{len(review) or 'nothing'} waiting for your merge"
                 if review else
                 "  📋 Review queue (awaiting-merge) — nothing waiting")
    for item in review:
        link = f" → {item['pr_url']}" if item["pr_url"] else " → (no PR link recorded)"
        # A link the delivery never recorded is a guess off the thread's prose —
        # say so, so the operator knows which links the worker stands behind.
        if item["pr_url"] and item.get("pr_source") == "legacy-free-text":
            link += "  (legacy: read from free text, no delivered marker)"
        if item["orphan"]:
            flag = "  ⚠️  PR looks merged — close it?"
        elif item.get("merge_state_unknown"):
            flag = "  ℹ️  merge-state unknown (non-GitHub delivery)"
        else:
            flag = ""
        lines.append(f"     #{item['number']}  {item['title']}{link}{flag}")
    lines.append("")

    decision = report["decision_queue"]
    lines.append(f"  ❓ Decision queue (needs-decision) — "
                 f"{len(decision)} waiting for your call"
                 if decision else
                 "  ❓ Decision queue (needs-decision) — nothing waiting")
    for item in decision:
        lines.append(f"     #{item['number']}  {item['title']}  (options in thread)")
    lines.append("")

    in_flight = report["in_flight"]
    lines.append(f"  ⚙️  In flight (in-progress) — {len(in_flight)} running"
                 if in_flight else
                 "  ⚙️  In flight (in-progress) — nothing running")
    for item in in_flight:
        worker = item["worker"] or "unknown"
        age = format_age(item["heartbeat_age_seconds"])
        msg = item.get("heartbeat_msg")
        note = f" — {msg}" if msg else ""
        # A silent lease reads exactly like a busy one, and nothing repairs it on
        # a schedule now, so say which one it is.
        flag = "  ⚠️  lease expired — the next drain steals it" if item.get("stale") else ""
        lines.append(f"     #{item['number']}  {item['title']}  · worker {worker} "
                     f"· lease renewed {age} ago{note}{flag}")
    lines.append("")

    hygiene = report.get("queue_hygiene") or []
    if hygiene:
        lines.append(f"  🧹 Queue hygiene — {len(hygiene)} task(s) a worker "
                     f"cannot start as filed")
        for item in hygiene:
            lines.append(f"     #{item['number']}  {item['title']}  "
                         f"· missing: {', '.join(item['missing'])}")
        lines.append("     A task with no project: label is invisible to every "
                     "worker; one with no Goal or Acceptance stalls on arrival.")
        lines.append("")

    orphans = report["orphans"]
    if orphans:
        joined = ", ".join(f"#{n}" for n in orphans)
        lines.append(f"  ⚠️  {len(orphans)} possible orphan(s): {joined} — "
                     f"PR looks merged but the issue is still open. You decide.")
        lines.append("")

    if project is None:
        projects = report["projects"]
        if projects:
            lines.append("  🚀 Launch — one worker per prepared environment")
            for name in projects:
                lines.append(f"     /kraken:unleash {repo} "
                             f"--worker-name <worker-name> --project {name}")
        else:
            lines.append("  🚀 Launch — no project: labels yet "
                         "(create one with init --project)")
    return "\n".join(lines)


def render_init(report: Json) -> str:
    """Human-facing init report: one line per repo/asset/label decision, then a
    summary line the skill can echo verbatim."""
    lines = [f"init: repo {report['repo']} ({report['repo_status']})"]
    for asset in report["assets"]:
        lines.append(f"init: asset {asset['path']} ({asset['status']})")
    for name in report["labels"]:
        lines.append(f"init: label {name} (upserted)")
    count = lambda s: sum(1 for a in report["assets"] if a["status"] == s)
    lines.append(
        f"init: done repo={report['repo']} repo_status={report['repo_status']} "
        f"assets_created={count('created')} assets_present={count('present')} "
        f"assets_removed={count('removed')} labels={len(report['labels'])}"
    )
    return "\n".join(lines)
