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


def get_or_create_suite(project_code: str, suite_title: str) -> int:
    """Create a test suite named after the Jira task ID and return its ID."""
    headers = get_headers()
    payload = {"title": suite_title}
    resp = requests.post(f"{BASE_URL}/suite/{project_code}", json=payload, headers=headers)
    resp.raise_for_status()
    suite_id = resp.json().get("result", {}).get("id")
    print(f"Created suite '{suite_title}' → id={suite_id}")
    return suite_id


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

    with open(args.test_cases) as f:
        data = json.load(f)

    test_cases = data["test_cases"]

    # Prefer approved_ids from the Slack button payload (reliable cross-run state)
    # Fall back to artifact state only if not provided
    if args.approved_ids:
        approved_ids = json.loads(args.approved_ids)
        print(f"Using approved_ids from Slack payload: {approved_ids}")
    else:
        approved_ids = data.get("approved_ids", [])
        print(f"Using approved_ids from artifact: {approved_ids}")

    if args.mode == "push":
        print(f"Pushing approved test cases to Qase for {args.jira_task}...")
        ids = push_test_cases(project_code, test_cases, approved_ids, args.jira_task)

        data["qase_ids"] = ids
        with open(args.test_cases, "w") as f:
            json.dump(data, f, indent=2)

        print(f"\nDone. Created {len(ids)} test case(s): {ids}")

    elif args.mode == "create-test-run":
        ids = data.get("qase_ids", [])
        if not ids:
            print("No Qase IDs found — run push first.", file=sys.stderr)
            sys.exit(1)
        create_test_run(project_code, args.jira_task, ids)


if __name__ == "__main__":
    main()
