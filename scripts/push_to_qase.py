"""
Pushes approved test cases to Qase via REST API v1.
Also handles test run creation.

API docs: https://developers.qase.io/reference
Base URL: https://api.qase.io/v1
Auth: Token <QASE_API_TOKEN> header
"""

import argparse
import json
import os
import sys
import requests

BASE_URL = "https://api.qase.io/v1"


def get_headers() -> dict:
    return {
        "Token": os.environ["QASE_API_TOKEN"],
        "Content-Type": "application/json",
    }


def find_suite_id(project_code: str, suite_title: str):
    """Return the ID of an existing suite with this exact title, or None."""
    headers = get_headers()
    # Qase's `filters[search]` is fuzzy/full-text, so we still match by
    # exact title in code. Scan up to 1000 suites (10 pages of 100).
    for offset in range(0, 1000, 100):
        resp = requests.get(
            f"{BASE_URL}/suite/{project_code}",
            params={"limit": 100, "offset": offset, "filters[search]": suite_title},
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {}) or {}
        entities = result.get("entities", []) or []
        for suite in entities:
            if suite.get("title") == suite_title:
                return suite.get("id")
        if len(entities) < 100:
            break
    return None


def get_or_create_suite(project_code: str, suite_title: str) -> int:
    """Return the ID of the suite named `suite_title`, creating it only if
    no existing suite has that exact title. This keeps repeated generations
    for the same Jira task appending to a single suite instead of spawning
    duplicates each time.
    """
    existing = find_suite_id(project_code, suite_title)
    if existing is not None:
        print(f"Reusing existing suite '{suite_title}' → id={existing}")
        return existing

    headers = get_headers()
    payload = {"title": suite_title}
    resp = requests.post(f"{BASE_URL}/suite/{project_code}", json=payload, headers=headers)
    resp.raise_for_status()
    suite_id = resp.json().get("result", {}).get("id")
    print(f"Created new suite '{suite_title}' → id={suite_id}")
    return suite_id


def list_cases_in_suite(project_code: str, suite_id: int) -> list:
    """Return all case IDs that currently live in the given suite."""
    headers = get_headers()
    case_ids = []
    for offset in range(0, 1000, 100):
        resp = requests.get(
            f"{BASE_URL}/case/{project_code}",
            params={"limit": 100, "offset": offset, "filters[suite_id]": suite_id},
            headers=headers,
        )
        resp.raise_for_status()
        result = resp.json().get("result", {}) or {}
        entities = result.get("entities", []) or []
        for case in entities:
            cid = case.get("id")
            if cid is not None:
                case_ids.append(cid)
        if len(entities) < 100:
            break
    return case_ids


def push_test_cases(project_code: str, test_cases: list, approved_ids: list,
                    jira_task_id: str) -> list:
    """Create only approved test cases in Qase inside a suite named after the Jira task."""
    headers = get_headers()
    created_ids = []

    # Fix: always filter strictly by approved_ids — never push rejected ones
    approved_set = set(approved_ids)
    to_push = [tc for tc in test_cases if tc["id"] in approved_set]

    if not to_push:
        print("No approved test cases to push. Approve at least one in Slack first.", file=sys.stderr)
        sys.exit(1)

    # Create a suite named after the Jira task ID
    suite_id = get_or_create_suite(project_code, jira_task_id)

    for tc in to_push:
        steps = [
            {
                "action": step,
                "expected_result": tc["expected_result"] if i == len(tc["steps"]) - 1 else "",
                "position": i + 1,
            }
            for i, step in enumerate(tc["steps"])
        ]

        payload = {
            "title": tc["title"],
            "preconditions": tc.get("preconditions", ""),
            "steps": steps,
            "suite_id": suite_id,
            "type": 1,       # other
            "priority": 2,   # medium
            "severity": 3,   # normal
        }

        resp = requests.post(
            f"{BASE_URL}/case/{project_code}",
            json=payload,
            headers=headers,
        )

        if resp.status_code in (200, 201):
            case_id = resp.json().get("result", {}).get("id")
            created_ids.append(case_id)
            print(f"  ✅ Created: #{case_id} — {tc['title']}")
        else:
            print(f"  ❌ ERROR creating '{tc['title']}': {resp.status_code} {resp.text}", file=sys.stderr)
            resp.raise_for_status()

    return created_ids


def create_test_run(project_code: str, jira_task_id: str, case_ids: list) -> str:
    """Create a test run in Qase with the given test case IDs."""
    headers = get_headers()

    payload = {
        "title": f"Test Run — {jira_task_id}",
        "cases": case_ids,
    }

    resp = requests.post(
        f"{BASE_URL}/run/{project_code}",
        json=payload,
        headers=headers,
    )
    resp.raise_for_status()

    run_id = resp.json().get("result", {}).get("id")
    print(f"Created test run: #{run_id}")
    return str(run_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="push", choices=["push", "create-test-run"])
    parser.add_argument("--test-cases", required=True)
    parser.add_argument("--jira-task", required=True)
    parser.add_argument("--approved-ids", help="JSON array of approved TC IDs from Slack button payload")
    args = parser.parse_args()

    project_code = os.environ.get("QASE_PROJECT_CODE", args.jira_task.split("-")[0])

    if args.mode == "push":
        with open(args.test_cases) as f:
            data = json.load(f)
        test_cases = data["test_cases"]

        # Prefer approved_ids from the Slack button payload (reliable cross-run state).
        # toJson(null) renders as the string "null" — guard for that.
        approved_ids = None
        if args.approved_ids and args.approved_ids.strip().lower() != "null":
            try:
                approved_ids = json.loads(args.approved_ids)
            except json.JSONDecodeError:
                approved_ids = None

        if approved_ids:
            print(f"Using approved_ids from Slack payload: {approved_ids}")
        else:
            approved_ids = data.get("approved_ids", [])
            print(f"Using approved_ids from artifact fallback: {approved_ids}")

        print(f"Pushing approved test cases to Qase for {args.jira_task}...")
        ids = push_test_cases(project_code, test_cases, approved_ids, args.jira_task)

        data["qase_ids"] = ids
        with open(args.test_cases, "w") as f:
            json.dump(data, f, indent=2)

        print(f"\nDone. Created {len(ids)} test case(s): {ids}")

    elif args.mode == "create-test-run":
        # Source of truth = Qase itself. The artifact-bound qase_ids never
        # survive cross-run (artifacts get re-uploaded to the wrong run id),
        # so we just ask Qase what's in the suite right now.
        suite_id = find_suite_id(project_code, args.jira_task)
        if suite_id is None:
            print(f"No Qase suite named '{args.jira_task}'. Push approved test cases first.",
                  file=sys.stderr)
            sys.exit(1)

        case_ids = list_cases_in_suite(project_code, suite_id)
        if not case_ids:
            print(f"Suite '{args.jira_task}' has no test cases. Push approved test cases first.",
                  file=sys.stderr)
            sys.exit(1)

        print(f"Found {len(case_ids)} case(s) in suite '{args.jira_task}': {case_ids}")
        create_test_run(project_code, args.jira_task, case_ids)


if __name__ == "__main__":
    main()
