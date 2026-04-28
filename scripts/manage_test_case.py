"""
Approves or rejects an individual test case in the artifact JSON.
Approved TCs stay visible but marked; rejected TCs are hidden from the Slack message.
"""

import argparse
import json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-cases", required=True)
    parser.add_argument("--action", required=True, choices=["approve", "reject"])
    parser.add_argument("--tc-id", required=True, type=int)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.test_cases) as f:
        data = json.load(f)

    approved_ids = set(data.get("approved_ids", []))
    rejected_ids = set(data.get("rejected_ids", []))

    if args.action == "approve":
        approved_ids.add(args.tc_id)
        rejected_ids.discard(args.tc_id)
        print(f"Approved TC-{args.tc_id}")
    else:
        rejected_ids.add(args.tc_id)
        approved_ids.discard(args.tc_id)
        print(f"Rejected TC-{args.tc_id}")

    data["approved_ids"] = list(approved_ids)
    data["rejected_ids"] = list(rejected_ids)

    with open(args.output, "w") as f:
        json.dump(data, f, indent=2)


if __name__ == "__main__":
    main()
