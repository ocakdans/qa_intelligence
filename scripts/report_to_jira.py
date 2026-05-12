"""
Three-way Jira reporter for a Qase test run.

Modes
-----
* `auto-router` (default)
    Read the latest Qase run for the Jira task. If every case has been
    executed AND there are any failures, post a Slack prompt asking the
    user how to handle them (bug tickets vs. plain comment). Otherwise
    fall through to `comment`.

* `comment`
    Post a single wiki-markup summary as a comment on the Jira issue.
    No bug tickets are created.

* `bug-tickets`
    Create one Jira "Bug" issue per failed case, with the test steps,
    expected result, tester comment, and any Qase attachments copied
    over. Then post a summary comment on the parent issue linking to
    every bug created.

Auth / config (env)
    QASE_API_TOKEN
    QASE_PROJECT_CODE        (defaults to the prefix of --jira-task)
    JIRA_BASE_URL
    JIRA_EMAIL
    JIRA_API_TOKEN
    SLACK_BOT_TOKEN
    SLACK_CHANNEL_ID
"""

import argparse
import base64
import json
import os
import sys
import requests
from slack_sdk import WebClient

QASE_BASE = "https://api.qase.io/v1"

STATUS_ORDER = [
    "passed", "failed", "blocked", "skipped",
    "retest", "invalid", "in_progress", "untested",
]
STATUS_EMOJI = {
    "passed": "✅",
    "failed": "❌",
    "blocked": "⏸️",
    "skipped": "⏭️",
    "retest": "🔄",
    "invalid": "⚠️",
    "in_progress": "🔵",
    "untested": "⚪",
}
EXECUTED_STATUSES = {"passed", "failed", "blocked", "skipped", "invalid"}


# ── HTTP helpers ──────────────────────────────────────────────────────────

def qase_headers():
    return {"Token": os.environ["QASE_API_TOKEN"], "Content-Type": "application/json"}


def jira_basic_auth():
    return base64.b64encode(
        f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}".encode()
    ).decode()


def jira_headers():
    return {
        "Authorization": "Basic " + jira_basic_auth(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ── Qase reads ────────────────────────────────────────────────────────────

def find_suite_id(project_code, suite_title):
    for offset in range(0, 1000, 100):
        resp = requests.get(
            f"{QASE_BASE}/suite/{project_code}",
            params={"limit": 100, "offset": offset, "filters[search]": suite_title},
            headers=qase_headers(),
        )
        resp.raise_for_status()
        entities = (resp.json().get("result") or {}).get("entities") or []
        for s in entities:
            if s.get("title") == suite_title:
                return s.get("id")
        if len(entities) < 100:
            break
    return None


def list_cases_in_suite(project_code, suite_id):
    """Return {case_id: title}."""
    cases = {}
    for offset in range(0, 1000, 100):
        resp = requests.get(
            f"{QASE_BASE}/case/{project_code}",
            params={"limit": 100, "offset": offset, "filters[suite_id]": suite_id},
            headers=qase_headers(),
        )
        resp.raise_for_status()
        entities = (resp.json().get("result") or {}).get("entities") or []
        for c in entities:
            if c.get("id") is not None:
                cases[c["id"]] = c.get("title", f"Case #{c['id']}")
        if len(entities) < 100:
            break
    return cases


def get_case_detail(project_code, case_id):
    """Full case definition: steps, preconditions, expected_result."""
    resp = requests.get(
        f"{QASE_BASE}/case/{project_code}/{case_id}",
        headers=qase_headers(),
    )
    resp.raise_for_status()
    return resp.json().get("result") or {}


def find_latest_run(project_code, jira_task_id):
    candidates = []
    for offset in range(0, 1000, 100):
        resp = requests.get(
            f"{QASE_BASE}/run/{project_code}",
            params={"limit": 100, "offset": offset, "filters[search]": jira_task_id},
            headers=qase_headers(),
        )
        resp.raise_for_status()
        entities = (resp.json().get("result") or {}).get("entities") or []
        for r in entities:
            if jira_task_id in (r.get("title") or ""):
                candidates.append(r)
        if len(entities) < 100:
            break
    if not candidates:
        return None
    candidates.sort(key=lambda r: r.get("id", 0), reverse=True)
    return candidates[0]


def _result_recency_key(r):
    """Bigger = more recent. Works for both ISO strings and unix ints."""
    for k in ("end_time", "updated_at", "created_at"):
        v = r.get(k)
        if v is not None:
            return str(v)
    return ""


def get_run_results(project_code, run_id):
    """Per-case LATEST result, keyed by case_id.

    A case can be re-executed many times within a single Qase run; the API
    returns every execution as its own row. We must pick the most-recent
    one per case_id, otherwise re-runs are invisible to the report.
    """
    raw = []
    for offset in range(0, 1000, 100):
        resp = requests.get(
            f"{QASE_BASE}/result/{project_code}",
            params={"limit": 100, "offset": offset, "filters[run]": run_id},
            headers=qase_headers(),
        )
        resp.raise_for_status()
        entities = (resp.json().get("result") or {}).get("entities") or []
        raw.extend(entities)
        if len(entities) < 100:
            break

    by_case = {}
    for r in raw:
        cid = r.get("case_id")
        if cid is None:
            continue
        prev = by_case.get(cid)
        if prev is None or _result_recency_key(r) > _result_recency_key(prev):
            by_case[cid] = r

    # Loud logging so a stale-data complaint is easy to debug from the
    # workflow log — you can see exactly which result hash we picked per
    # case and when it was executed.
    print(f"Fetched {len(raw)} raw result row(s); kept latest per case → "
          f"{len(by_case)} case(s).")
    for cid in sorted(by_case.keys()):
        r = by_case[cid]
        print(f"  case #{cid}: status={r.get('status')!r} "
              f"end_time={r.get('end_time')} "
              f"hash={(r.get('hash') or '')[:10]}")
    return by_case


def fetch_attachment_bytes(att):
    """Qase results may attach files. Returns (filename, mime, bytes) or None."""
    url = att.get("url")
    if not url:
        hash_ = att.get("hash")
        if not hash_:
            return None
        meta = requests.get(f"{QASE_BASE}/attachment/{hash_}", headers=qase_headers())
        if not meta.ok:
            return None
        url = (meta.json().get("result") or {}).get("url")
        if not url:
            return None
    # The signed URL doesn't need the Qase token.
    resp = requests.get(url, timeout=30)
    if not resp.ok:
        return None
    return (
        att.get("filename") or "attachment.bin",
        att.get("mime") or "application/octet-stream",
        resp.content,
    )


def collect_result_attachments(result):
    """All attachments on a result, including ones nested under steps."""
    out = list(result.get("attachments") or [])
    for step in result.get("steps") or []:
        out.extend(step.get("attachments") or [])
    return out


# ── Jira writes ───────────────────────────────────────────────────────────

def jira_verify_or_raise(jira_base_url, issue_key):
    """Run /myself + /issue first so errors are legible."""
    base = jira_base_url.rstrip("/")
    me = requests.get(f"{base}/rest/api/2/myself", headers=jira_headers())
    if me.status_code in (401, 403):
        print(f"Jira auth failed: HTTP {me.status_code}. Check JIRA_EMAIL/JIRA_API_TOKEN.",
              file=sys.stderr)
        me.raise_for_status()
    me.raise_for_status()

    issue = requests.get(
        f"{base}/rest/api/2/issue/{issue_key}?fields=summary",
        headers=jira_headers(),
    )
    if issue.status_code == 404:
        print(f"Jira issue {issue_key} not visible. "
              "Confirm the key exists and the JIRA_EMAIL user can browse it.",
              file=sys.stderr)
        issue.raise_for_status()
    issue.raise_for_status()


def post_jira_comment(jira_base_url, issue_key, body_text):
    url = f"{jira_base_url.rstrip('/')}/rest/api/2/issue/{issue_key}/comment"
    resp = requests.post(url, json={"body": body_text}, headers=jira_headers())
    if resp.status_code not in (200, 201):
        print(f"Jira comment POST failed: HTTP {resp.status_code}",
              file=sys.stderr)
        print(f"Response body: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def create_jira_bug(jira_base_url, project_key, summary, description, labels=None):
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary[:250],  # Jira limit
            "issuetype": {"name": "Bug"},
            "description": description,
            "labels": labels or ["qa-automation", "from-qase"],
        }
    }
    url = f"{jira_base_url.rstrip('/')}/rest/api/2/issue"
    resp = requests.post(url, json=payload, headers=jira_headers())
    if resp.status_code not in (200, 201):
        print(f"Jira bug create failed: HTTP {resp.status_code} {resp.text[:500]}",
              file=sys.stderr)
        resp.raise_for_status()
    return resp.json().get("key")


def attach_to_jira(jira_base_url, issue_key, filename, content_bytes, mime_type):
    """Multipart attachment upload. Needs X-Atlassian-Token: no-check."""
    headers = {
        "Authorization": "Basic " + jira_basic_auth(),
        "X-Atlassian-Token": "no-check",
        "Accept": "application/json",
    }
    files = {"file": (filename, content_bytes, mime_type)}
    url = f"{jira_base_url.rstrip('/')}/rest/api/2/issue/{issue_key}/attachments"
    resp = requests.post(url, headers=headers, files=files, timeout=60)
    if not resp.ok:
        print(f"Attach failed for {filename} on {issue_key}: "
              f"HTTP {resp.status_code} {resp.text[:300]}", file=sys.stderr)
        return False
    return True


def link_issues(jira_base_url, inward_key, outward_key, link_type="Relates"):
    """Try to relate bug to parent. Best-effort — skip silently if link type missing."""
    payload = {
        "type": {"name": link_type},
        "inwardIssue": {"key": inward_key},
        "outwardIssue": {"key": outward_key},
    }
    url = f"{jira_base_url.rstrip('/')}/rest/api/2/issueLink"
    resp = requests.post(url, json=payload, headers=jira_headers())
    if not resp.ok:
        print(f"Issue link {inward_key} → {outward_key} failed: HTTP {resp.status_code}",
              file=sys.stderr)


# ── Slack helpers ─────────────────────────────────────────────────────────

def slack_client():
    return WebClient(token=os.environ["SLACK_BOT_TOKEN"])


def post_failure_action_prompt(channel, message_ts, jira_task_id, run_id, repo,
                               passed, failed, qase_run_url):
    """Two-button choice: create bug tickets or post a single comment."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🎉 *All test cases executed for {jira_task_id}.*\n"
                    f"✅ Passed: *{passed}*  ·  ❌ Failed: *{failed}*\n"
                    f"<{qase_run_url}|View run in Qase>\n\n"
                    f"How should I handle the *{failed} failed* case(s)?"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"qa_failure_action_{run_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🐞 Create Bug Tickets"},
                    "style": "danger",
                    "action_id": "qa_create_bug_tickets",
                    "value": json.dumps({
                        "run_id": run_id,
                        "jira_task_id": jira_task_id,
                        "repo": repo,
                    }),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📝 Post as Comment"},
                    "action_id": "qa_post_comment_only",
                    "value": json.dumps({
                        "run_id": run_id,
                        "jira_task_id": jira_task_id,
                        "repo": repo,
                    }),
                },
            ],
        },
    ]
    slack_client().chat_postMessage(
        channel=channel, thread_ts=message_ts, blocks=blocks,
        text=f"{failed} failed case(s) for {jira_task_id} — choose how to report",
    )


def post_simple_thread_reply(channel, message_ts, text):
    slack_client().chat_postMessage(channel=channel, thread_ts=message_ts, text=text)


# ── Report builders ──────────────────────────────────────────────────────

def aggregate(results_by_case, all_case_ids):
    """Return (status_counts, case_status). Untested cases default to 'untested'."""
    case_status = {}
    for cid, r in results_by_case.items():
        case_status[cid] = r.get("status") or "untested"
    for cid in all_case_ids:
        case_status.setdefault(cid, "untested")
    counts = {}
    for s in case_status.values():
        counts[s] = counts.get(s, 0) + 1
    return counts, case_status


def build_summary_comment(project_code, jira_task_id, run, results_by_case, case_titles):
    qase_run_url = f"https://app.qase.io/run/{project_code}/dashboard/{run['id']}"
    counts, case_status = aggregate(results_by_case, case_titles.keys())
    total = sum(counts.values())
    run_title = run.get("title") or f"Run #{run['id']}"

    lines = [
        f"h2. 🧪 Test Report — {jira_task_id}",
        "",
        f"*Test Run:* [{run_title}|{qase_run_url}]",
        "",
        "h3. 📊 Summary",
        "||Status||Count||",
    ]
    for status in STATUS_ORDER:
        n = counts.get(status, 0)
        if n:
            lines.append(
                f"|{STATUS_EMOJI.get(status, '•')} "
                f"{status.replace('_', ' ').title()}|{n}|"
            )
    lines.append(f"|*Total*|*{total}*|")
    lines.append("")
    lines.append("h3. 📋 Per-case Results")
    for cid in sorted(case_titles.keys()):
        status = case_status.get(cid, "untested")
        emoji = STATUS_EMOJI.get(status, "•")
        case_url = f"https://app.qase.io/case/{project_code}-{cid}"
        lines.append(f"* {emoji} [{project_code}-{cid}|{case_url}] — {case_titles[cid]}")
    return "\n".join(lines)


def build_bug_description(project_code, parent_key, qase_run_url, case, result):
    case_url = f"https://app.qase.io/case/{project_code}-{case['id']}"
    steps_md = ""
    case_steps = case.get("steps") or []
    if case_steps:
        steps_md = "\n".join(f"# {s.get('action', '')}" for s in case_steps)
    else:
        steps_md = "_No steps recorded._"

    expected = case.get("expected_result")
    if not expected and case_steps:
        # Some cases carry expected only on the last step.
        expected = case_steps[-1].get("expected_result") or ""
    if not expected:
        expected = "_None recorded._"

    comment = result.get("comment") or "_No tester comment._"
    preconditions = case.get("preconditions") or "_None._"

    return "\n".join([
        "*Auto-generated from a failed Qase test execution.*",
        "",
        f"*Parent issue:* {parent_key}",
        f"*Qase case:* [{project_code}-{case['id']}|{case_url}]",
        f"*Qase test run:* [Open in Qase|{qase_run_url}]",
        "",
        "h3. Preconditions",
        preconditions,
        "",
        "h3. Steps",
        steps_md,
        "",
        "h3. Expected Result",
        expected,
        "",
        "h3. Tester's Comment",
        comment,
        "",
        "h3. Status",
        "❌ Failed",
    ])


def build_bug_summary_comment(jira_task_id, qase_run_url, created):
    """Comment on parent issue after bugs are created."""
    lines = [
        f"h2. 🐞 Bug tickets created for failed cases — {jira_task_id}",
        "",
        f"*Source:* [Qase test run|{qase_run_url}]",
        "",
        "||Bug||Failed Case||",
    ]
    for entry in created:
        lines.append(f"|{entry['bug_key']}|{entry['case_title']}|")
    return "\n".join(lines)


# ── Mode runners ──────────────────────────────────────────────────────────

def run_comment_mode(args, ctx):
    body = build_summary_comment(
        ctx["project_code"], args.jira_task, ctx["run"],
        ctx["results"], ctx["case_titles"],
    )
    print("─── Comment body ─────────────────────")
    print(body)
    print("──────────────────────────────────────")
    post_jira_comment(ctx["jira_base_url"], args.jira_task, body)
    print(f"✅ Posted report comment on {args.jira_task}")

    if args.message_ts:
        post_simple_thread_reply(
            os.environ["SLACK_CHANNEL_ID"], args.message_ts,
            f"📊 Test report posted on <{ctx['jira_base_url']}/browse/{args.jira_task}|{args.jira_task}>.",
        )


def run_bug_tickets_mode(args, ctx):
    """Create one Jira bug per failed case, then summary comment on the parent."""
    project_key = args.jira_task.split("-")[0]
    qase_run_url = f"https://app.qase.io/run/{ctx['project_code']}/dashboard/{ctx['run']['id']}"

    failures = [
        (cid, ctx["results"][cid]) for cid in ctx["case_titles"]
        if ctx["results"].get(cid) and ctx["results"][cid].get("status") == "failed"
    ]
    if not failures:
        msg = f"No failed cases on the latest run for {args.jira_task}. Nothing to do."
        print(msg)
        if args.message_ts:
            post_simple_thread_reply(
                os.environ["SLACK_CHANNEL_ID"], args.message_ts, "✅ " + msg,
            )
        return

    created = []
    for case_id, result in failures:
        case = get_case_detail(ctx["project_code"], case_id) or {"id": case_id}
        case.setdefault("id", case_id)
        title = case.get("title") or ctx["case_titles"].get(case_id, f"Case #{case_id}")
        summary = f"[QA] {title} — failed in {args.jira_task}"
        description = build_bug_description(
            ctx["project_code"], args.jira_task, qase_run_url, case, result,
        )
        bug_key = create_jira_bug(ctx["jira_base_url"], project_key, summary, description)
        print(f"  🐞 Created {bug_key} for case #{case_id} — {title}")

        # Attach any Qase result attachments to the new bug (best-effort).
        for att in collect_result_attachments(result):
            payload = fetch_attachment_bytes(att)
            if not payload:
                continue
            fname, mime, content = payload
            ok = attach_to_jira(ctx["jira_base_url"], bug_key, fname, content, mime)
            if ok:
                print(f"     attached {fname}")

        # Best-effort link back to the parent story.
        link_issues(ctx["jira_base_url"], bug_key, args.jira_task)

        created.append({"bug_key": bug_key, "case_id": case_id, "case_title": title})

    summary_body = build_bug_summary_comment(args.jira_task, qase_run_url, created)
    post_jira_comment(ctx["jira_base_url"], args.jira_task, summary_body)
    print(f"✅ Created {len(created)} bug ticket(s) and posted summary on {args.jira_task}")

    if args.message_ts:
        bug_links = ", ".join(
            f"<{ctx['jira_base_url']}/browse/{c['bug_key']}|{c['bug_key']}>"
            for c in created
        )
        post_simple_thread_reply(
            os.environ["SLACK_CHANNEL_ID"], args.message_ts,
            f"🐞 Created {len(created)} bug ticket(s) for *{args.jira_task}*: {bug_links}",
        )


def run_router_mode(args, ctx):
    counts, _ = aggregate(ctx["results"], ctx["case_titles"].keys())
    failed = counts.get("failed", 0)
    untested = counts.get("untested", 0) + counts.get("in_progress", 0)
    qase_run_url = f"https://app.qase.io/run/{ctx['project_code']}/dashboard/{ctx['run']['id']}"

    print(f"Router decision input: failed={failed}, not-yet-executed={untested}, "
          f"all_counts={counts}")

    # The user wants the prompt only when execution is 100% complete AND
    # there are failures to make a decision about. Any other path falls
    # straight through to the existing comment behavior.
    if untested == 0 and failed > 0 and args.message_ts:
        passed = counts.get("passed", 0)
        post_failure_action_prompt(
            os.environ["SLACK_CHANNEL_ID"], args.message_ts,
            args.jira_task, args.run_id or "manual",
            args.repo or os.environ.get("GITHUB_REPOSITORY", ""),
            passed, failed, qase_run_url,
        )
        print(f"Posted failure-action prompt to Slack ({failed} failures).")
        return

    # Default: post the comment immediately.
    run_comment_mode(args, ctx)


# ── Entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="auto-router",
                        choices=["auto-router", "comment", "bug-tickets"])
    parser.add_argument("--jira-task", required=True)
    parser.add_argument("--message-ts", help="Slack message ts for thread replies / prompts.")
    parser.add_argument("--run-id", help="GitHub Actions run ID (used in Slack button payloads).")
    parser.add_argument("--repo", help="GitHub repo (used in Slack button payloads).")
    args = parser.parse_args()

    project_code = os.environ.get("QASE_PROJECT_CODE") or args.jira_task.split("-")[0]
    jira_base_url = os.environ["JIRA_BASE_URL"]

    jira_verify_or_raise(jira_base_url, args.jira_task)

    suite_id = find_suite_id(project_code, args.jira_task)
    if suite_id is None:
        print(f"No Qase suite '{args.jira_task}' found. Push approved test cases first.",
              file=sys.stderr)
        sys.exit(1)

    case_titles = list_cases_in_suite(project_code, suite_id)
    if not case_titles:
        print(f"Suite '{args.jira_task}' has no cases.", file=sys.stderr)
        sys.exit(1)

    run = find_latest_run(project_code, args.jira_task)
    if run is None:
        print(f"No Qase test run found for '{args.jira_task}'. Create one first.",
              file=sys.stderr)
        sys.exit(1)
    print(f"Latest Qase run picked: #{run['id']} — {run.get('title')!r} "
          f"(status={run.get('status_text') or run.get('status')}, "
          f"created_at={run.get('start_time') or run.get('created_at')})")

    results = get_run_results(project_code, run["id"])

    ctx = {
        "project_code": project_code,
        "jira_base_url": jira_base_url,
        "run": run,
        "results": results,
        "case_titles": case_titles,
    }

    if args.mode == "auto-router":
        run_router_mode(args, ctx)
    elif args.mode == "comment":
        run_comment_mode(args, ctx)
    elif args.mode == "bug-tickets":
        run_bug_tickets_mode(args, ctx)


if __name__ == "__main__":
    main()
