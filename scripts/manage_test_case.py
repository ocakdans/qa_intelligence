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
    parser.add_argument("--existing-approved-ids",
                        help="JSON array of previously approved IDs carried in the Slack button payload")
    args = parser.parse_args()

    with open(args.test_cases) as f:
        data = json.load(f)

    # Prefer IDs from the Slack button payload (accumulates across runs) over the artifact.
    # toJson(null) renders as the literal string "null" — guard for that.
    payload_ids = None
    if args.existing_approved_ids and args.existing_approved_ids.strip().lower() != "null":
        try:
            payload_ids = json.loads(args.existing_approved_ids)
        except json.JSONDecodeError:
            payload_ids = None

    if payload_ids is not None:
        approved_ids = set(payload_ids)
        print(f"Seeding approved_ids from Slack payload: {approved_ids}")
    else:
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
