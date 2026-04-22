"""
Pushes approved test cases to Zephyr Advanced via Jira REST API.
Also handles test run creation.
"""

import argparse
import json
import os
import sys
import requests
from requests.auth import HTTPBasicAuth


def get_auth_and_headers() -> tuple[HTTPBasicAuth, dict]:
    auth = HTTPBasicAuth(os.environ["JIRA_EMAIL"], os.environ["JIRA_API_TOKEN"])
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    return auth, headers


def push_test_cases(base_url: str, jira_task_id: str, test_cases: list) -> list:
    auth, headers = get_auth_and_headers()
    created_keys = []

    for tc in test_cases:
        payload = {
            "projectKey": jira_task_id.split("-")[0],
            "name": tc["title"],
            "precondition": tc.get("preconditions", ""),
            "issueLinks": [jira_task_id],
            "labels": [tc.get("type", "functional")],
            "testScript": {
                "type": "STEP_BY_STEP",
                "steps": [
                    {"description": step, "expectedResult": tc["expected_result"] if i == len(tc["steps"]) - 1 else ""}
                    for i, step in enumerate(tc["steps"])
                ],
            },
        }

        url = f"{base_url.rstrip('/')}/rest/atm/1.0/testcase"
        resp = requests.post(url, json=payload, auth=auth, headers=headers)

        if resp.status_code in (200, 201):
            key = resp.json().get("key", "unknown")
            created_keys.append(key)
            print(f"  Created test case: {key} — {tc['title']}")
        else:
            print(f"  ERROR creating '{tc['title']}': {resp.status_code} {resp.text}", file=sys.stderr)
            resp.raise_for_status()

    return created_keys


def create_test_run(base_url: str, jira_task_id: str, test_case_keys: list) -> str:
    auth, headers = get_auth_and_headers()

    payload = {
        "projectKey": jira_task_id.split("-")[0],
        "name": f"Test Run — {jira_task_id}",
        "issueKey": jira_task_id,
        "items": [{"testCaseKey": key} for key in test_case_keys],
    }

    url = f"{base_url.rstrip('/')}/rest/atm/1.0/testrun"
    resp = requests.post(url, json=payload, auth=auth, headers=headers)
    resp.raise_for_status()

    run_key = resp.json().get("key", "unknown")
    print(f"Created test run: {run_key}")
    return run_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="push", choices=["push", "create-test-run"])
    parser.add_argument("--test-cases", required=True)
    parser.add_argument("--jira-task", required=True)
    args = parser.parse_args()

    base_url = os.environ["JIRA_BASE_URL"]

    with open(args.test_cases) as f:
        data = json.load(f)

    test_cases = data["test_cases"]

    if args.mode == "push":
        print(f"Pushing {len(test_cases)} test case(s) to Zephyr for {args.jira_task}...")
        keys = push_test_cases(base_url, args.jira_task, test_cases)

        # Save keys back to file so create-test-run step can read them
        data["zephyr_keys"] = keys
        with open(args.test_cases, "w") as f:
            json.dump(data, f, indent=2)

        print(f"Done. Zephyr keys: {keys}")

    elif args.mode == "create-test-run":
        keys = data.get("zephyr_keys", [])
        if not keys:
            print("No Zephyr keys found in test_cases.json — push first.", file=sys.stderr)
            sys.exit(1)
        create_test_run(base_url, args.jira_task, keys)


if __name__ == "__main__":
    main()
