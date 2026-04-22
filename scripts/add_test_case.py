"""
Appends a manually written test case to the existing test_cases.json artifact.
Called when the user clicks "Add Test Case" in Slack and submits the modal.
"""

import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-cases", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--steps", required=True, help="Steps separated by ' | '")
    parser.add_argument("--expected", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.test_cases) as f:
        data = json.load(f)

    next_id = max((tc["id"] for tc in data["test_cases"]), default=0) + 1
    steps = [s.strip() for s in args.steps.split("|") if s.strip()]

    data["test_cases"].append(
        {
            "id": next_id,
            "title": args.title,
            "preconditions": "",
            "steps": steps,
            "expected_result": args.expected,
            "type": "positive",
            "manual": True,
        }
    )

    with open(args.output, "w") as f:
        json.dump(data, f, indent=2)

    print(f"Added TC-{next_id}: {args.title}")


if __name__ == "__main__":
    main()
