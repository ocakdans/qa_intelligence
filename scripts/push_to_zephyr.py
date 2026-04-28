"""
Pushes approved test cases to Zephyr Scale Cloud via SmartBear REST API v2.
Also handles test cycle (run) creation.

API docs: https://support.smartbear.com/zephyr-scale-cloud/api-docs/
Base URL: https://api.zephyrscale.smartbear.com/v2
Auth: Bearer <ZEPHYR_API_TOKEN>
"""

import argparse
import json
import os
import sys
import requests

BASE_URL = "https://api.zephyrscale.smartbear.com/v2"


def get_headers() -> dict:
    token = os.environ["ZEPHYR_API_TOKEN"]
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def push_test_cases(project_key: str, test_cases: list, approved_ids: list) -> list:
    """Create approved test cases in Zephyr Scale and return their keys."""
    headers = get_headers()
    created_keys = []

    # Filter to only approved test cases (if any approvals; otherwise push all)
    to_push = [tc for tc in test_cases if tc["id"] in approved_ids] if approved_ids else test_cases

    if not to_push:
        print("No approved test cases to push.", file=sys.stderr)
        sys.exit(1)

    for tc in to_push:
        # Build step-by-step items
        steps = []
        for i, step in enumerate(tc["steps"]):
            steps.append({
                "inline": {
                    "description": step,
                    "testData": "",
                    "expectedResult": tc["expected_result"] if i == len(tc["steps"]) - 1 else "",
                }
            })

        payload = {
            "projectKey": project_key,
            "name": tc["title"],
            "precondition": tc.get("preconditions", ""),
            "labels": [tc.get("type", "functional")],
            "steps": {
                "mode": "OVERWRITE",
                "items": steps,
            },
        }

        resp = requests.post(f"{BASE_URL}/testcases", json=payload, headers=headers)

        if resp.status_code in (200, 201):
            key = resp.json().get("key", "unknown")
            created_keys.append(key)
            print(f"  ✅ Created: {key} — {tc['title']}")
        else:
            print(f"  ❌ ERROR creating '{tc['title']}': {resp.status_code} {resp.text}", file=sys.stderr)
            resp.raise_for_status()

    return created_keys


def create_test_cycle(project_key: str, jira_task_id: str, test_case_keys: list) -> str:
    """Create a test cycle and add test cases as executions."""
    headers = get_headers()

    # Create the cycle
    cycle_payload = {
        "projectKey": project_key,
        "name": f"Test Run — {jira_task_id}",
    }
    resp = requests.post(f"{BASE_URL}/testcycles", json=cycle_payload, headers=headers)
    resp.raise_for_status()
    cycle_key = resp.json().get("key", "unknown")
    print(f"Created test cycle: {cycle_key}")

    # Add each test case as an execution
    for tc_key in test_case_keys:
        exec_payload = {
            "projectKey": project_key,
            "testCycleKey": cycle_key,
            "testCaseKey": tc_key,
            "statusName": "Not Executed",
        }
        exec_resp = requests.post(f"{BASE_URL}/testexecutions", json=exec_payload, headers=headers)
        if exec_resp.status_code in (200, 201):
            print(f"  Added {tc_key} to cycle {cycle_key}")
        else:
            print(f"  Warning: could not add {tc_key}: {exec_resp.status_code} {exec_resp.text}")

    return cycle_key


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="push", choices=["push", "create-test-run"])
    parser.add_argument("--test-cases", required=True)
    parser.add_argument("--jira-task", required=True)
    args = parser.parse_args()

    project_key = args.jira_task.split("-")[0]

    with open(args.test_cases) as f:
        data = json.load(f)

    test_cases = data["test_cases"]
    approved_ids = data.get("approved_ids", [])

    if args.mode == "push":
        print(f"Pushing approved test cases to Zephyr for {args.jira_task}...")
        keys = push_test_cases(project_key, test_cases, approved_ids)

        data["zephyr_keys"] = keys
        with open(args.test_cases, "w") as f:
            json.dump(data, f, indent=2)

        print(f"\nDone. Created {len(keys)} test case(s): {keys}")

    elif args.mode == "create-test-run":
        keys = data.get("zephyr_keys", [])
        if not keys:
            print("No Zephyr keys found — run push first.", file=sys.stderr)
            sys.exit(1)
        create_test_cycle(project_key, args.jira_task, keys)


if __name__ == "__main__":
    main()
