"""
Posts a Qase test-run summary as a comment on the linked Jira issue.

Flow for a given Jira task ID (e.g. QCT-1):
  1. Find the Qase suite with that exact title.
  2. Find the most recent test run whose title contains the task ID.
  3. Read run results (per-case status) and case titles from Qase.
  4. Format a wiki-markup comment and POST it to /rest/api/2/issue/{key}/comment.

Qase docs: https://developers.qase.io/reference
Jira docs: https://developer.atlassian.com/cloud/jira/platform/rest/v2/
"""

import argparse
import base64
import os
import sys
import requests

QASE_BASE = "https://api.qase.io/v1"

# Display ordering in the report — passed first, untested last.
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


def qase_headers():
    return {"Token": os.environ["QASE_API_TOKEN"], "Content-Type": "application/json"}


def jira_headers():
    creds = f"{os.environ['JIRA_EMAIL']}:{os.environ['JIRA_API_TOKEN']}".encode()
    return {
        "Authorization": "Basic " + base64.b64encode(creds).decode(),
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


# ── Qase reads ────────────────────────────────────────────────────────────

def find_suite_id(project_code: str, suite_title: str):
    """Return the ID of an existing Qase suite with this exact title, or None."""
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


def list_cases_in_suite(project_code: str, suite_id: int) -> dict:
    """Return {case_id: title} for every case in the suite."""
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


def find_latest_run(project_code: str, jira_task_id: str):
    """Return the most-recently-created run whose title contains the task ID."""
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
    # Highest ID = most recent.
    candidates.sort(key=lambda r: r.get("id", 0), reverse=True)
    return candidates[0]


def get_run_results(project_code: str, run_id: int) -> list:
    """Return raw result entries for a run."""
    out = []
    for offset in range(0, 1000, 100):
        resp = requests.get(
            f"{QASE_BASE}/result/{project_code}",
            params={"limit": 100, "offset": offset, "filters[run]": run_id},
            headers=qase_headers(),
        )
        resp.raise_for_status()
        entities = (resp.json().get("result") or {}).get("entities") or []
        out.extend(entities)
        if len(entities) < 100:
            break
    return out


# ── Report formatter ──────────────────────────────────────────────────────

def build_report(project_code: str, jira_task_id: str, run: dict,
                 results: list, case_titles: dict) -> str:
    """Render the report body in Jira wiki markup."""
    qase_run_url = f"https://app.qase.io/run/{project_code}/dashboard/{run['id']}"

    # Per-case latest status (a case can have multiple result rows on retests;
    # the API returns them newest-first, so the first occurrence wins).
    case_status = {}
    for r in results:
        cid = r.get("case_id")
        if cid is None or cid in case_status:
            continue
        case_status[cid] = r.get("status") or "untested"

    # Anything in the suite that has no result row is reported as untested.
    for cid in case_titles:
        case_status.setdefault(cid, "untested")

    # Aggregate counts across the suite (not just rows that exist in results).
    counts = {}
    for status in case_status.values():
        counts[status] = counts.get(status, 0) + 1
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
            label = status.replace("_", " ").title()
            lines.append(f"|{STATUS_EMOJI.get(status, '•')} {label}|{n}|")
    lines.append(f"|*Total*|*{total}*|")
    lines.append("")
    lines.append("h3. 📋 Per-case Results")

    for cid in sorted(case_titles.keys()):
        status = case_status.get(cid, "untested")
        emoji = STATUS_EMOJI.get(status, "•")
        case_url = f"https://app.qase.io/case/{project_code}-{cid}"
        title = case_titles[cid]
        lines.append(f"* {emoji} [{project_code}-{cid}|{case_url}] — {title}")

    return "\n".join(lines)


# ── Jira write ────────────────────────────────────────────────────────────

def post_jira_comment(jira_base_url: str, issue_key: str, body_text: str):
    base = jira_base_url.rstrip("/")

    # Step 1: who am I? /myself returns 200 only when basic-auth credentials
    # are valid. This unambiguously distinguishes auth failures from issue
    # / project visibility problems (Atlassian Cloud returns 404 for both
    # "wrong creds" and "no permission to see issue").
    me = requests.get(f"{base}/rest/api/2/myself", headers=jira_headers())
    if me.status_code == 401 or me.status_code == 403:
        print(
            f"Jira auth failed: HTTP {me.status_code}. Check JIRA_EMAIL and "
            f"JIRA_API_TOKEN — the email must match the API token owner.",
            file=sys.stderr,
        )
        me.raise_for_status()
    if me.status_code == 404:
        # If even /myself is 404, the base URL itself is wrong.
        print(
            "Jira /myself returned 404. JIRA_BASE_URL is almost certainly "
            "wrong. It should be like 'https://your-tenant.atlassian.net' "
            "(no trailing slash, no /jira, no /wiki).",
            file=sys.stderr,
        )
        me.raise_for_status()
    me.raise_for_status()
    me_data = me.json()
    print(f"Jira auth OK as: {me_data.get('displayName')} ({me_data.get('emailAddress', 'email hidden')})")

    # Step 2: can we see this specific issue?
    probe_url = f"{base}/rest/api/2/issue/{issue_key}?fields=summary"
    probe = requests.get(probe_url, headers=jira_headers())
    if probe.status_code == 404:
        # Help the user figure out what project keys actually exist.
        try:
            projects_resp = requests.get(
                f"{base}/rest/api/2/project",
                headers=jira_headers(),
            )
            if projects_resp.ok:
                projects = projects_resp.json() or []
                listed = ", ".join(
                    f"{p.get('key')} ({p.get('name')})" for p in projects[:20]
                ) or "(none)"
                print(f"Jira projects visible to {me_data.get('emailAddress', 'you')}: {listed}",
                      file=sys.stderr)
        except Exception:
            pass
        print(
            f"Jira issue '{issue_key}' not found / not visible. Either:\n"
            f"  - The project key in '{issue_key}' is wrong (see the list above)\n"
            f"  - That specific issue number doesn't exist yet",
            file=sys.stderr,
        )
        probe.raise_for_status()
    probe.raise_for_status()
    print(f"Jira issue {issue_key} visible — '{probe.json().get('fields', {}).get('summary', '')}'")

    url = f"{base}/rest/api/2/issue/{issue_key}/comment"
    resp = requests.post(url, json={"body": body_text}, headers=jira_headers())
    if resp.status_code not in (200, 201):
        # Strip the body text so the print doesn't trigger secret-masking on
        # the URL, which would obscure the diagnostic.
        print(f"Jira comment POST failed: HTTP {resp.status_code}",
              file=sys.stderr)
        print(f"Response body: {resp.text[:500]}", file=sys.stderr)
        resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jira-task", required=True)
    args = parser.parse_args()

    project_code = os.environ.get("QASE_PROJECT_CODE") or args.jira_task.split("-")[0]
    jira_base_url = os.environ["JIRA_BASE_URL"]

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
        print(f"No Qase test run found for '{args.jira_task}'. Create a test run first.",
              file=sys.stderr)
        sys.exit(1)

    print(f"Reporting on Qase run #{run['id']} — {run.get('title')}")
    results = get_run_results(project_code, run["id"])

    body = build_report(project_code, args.jira_task, run, results, case_titles)
    print("─── Report body ─────────────────────")
    print(body)
    print("─────────────────────────────────────")

    post_jira_comment(jira_base_url, args.jira_task, body)
    print(f"✅ Posted report comment on Jira issue {args.jira_task}")


if __name__ == "__main__":
    main()
