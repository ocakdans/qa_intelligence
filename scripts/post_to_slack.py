"""
Handles all Slack messaging for the QA pipeline:
  - Initial test cases review message (per-TC Approve / Reject buttons)
  - Ask-test-run prompt (after push to Zephyr)
  - Status updates: rejected, test-run-created, test-run-skipped
"""

import argparse
import json
import os
from slack_sdk import WebClient

JIRA_BASE_URL = "https://selimocakdan.atlassian.net"


def build_test_case_blocks(data: dict, jira_task_id: str, run_id: str, repo: str) -> list:
    test_cases = data.get("test_cases", [])
    approved_ids = set(data.get("approved_ids", []))
    rejected_ids = set(data.get("rejected_ids", []))

    # Only show non-rejected test cases
    visible = [tc for tc in test_cases if tc["id"] not in rejected_ids]

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🧪 Test Cases — {jira_task_id}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*<{JIRA_BASE_URL}/browse/{jira_task_id}|{jira_task_id}>* · "
                    f"{len(visible)} test case(s) · Review each one below."
                ),
            },
        },
        {"type": "divider"},
    ]

    for tc in visible:
        is_approved = tc["id"] in approved_ids
        type_emoji = {"positive": "✅", "negative": "❌", "edge_case": "⚠️"}.get(tc["type"], "•")
        steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(tc["steps"]))
        status_prefix = "✅ *APPROVED* — " if is_approved else ""

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{status_prefix}{type_emoji} *TC-{tc['id']}: {tc['title']}*\n"
                        f"*Preconditions:* {tc.get('preconditions', 'None')}\n"
                        f"*Steps:*\n{steps_text}\n"
                        f"*Expected:* {tc['expected_result']}"
                    ),
                },
            }
        )

        # Only show buttons for pending test cases
        if not is_approved:
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"tc_{run_id}_{tc['id']}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "✅ Approve"},
                            "style": "primary",
                            "action_id": "qa_approve_tc",
                            "value": json.dumps({
                                "run_id": run_id,
                                "jira_task_id": jira_task_id,
                                "repo": repo,
                                "tc_id": tc["id"],
                            }),
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "❌ Reject"},
                            "style": "danger",
                            "action_id": "qa_reject_tc",
                            "value": json.dumps({
                                "run_id": run_id,
                                "jira_task_id": jira_task_id,
                                "repo": repo,
                                "tc_id": tc["id"],
                            }),
                        },
                    ],
                }
            )

        blocks.append({"type": "divider"})

    # Bottom action bar
    blocks.append(
        {
            "type": "actions",
            "block_id": f"qa_bottom_{run_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚀 Push Approved to Zephyr"},
                    "style": "primary",
                    "action_id": "qa_push_to_zephyr",
                    "value": json.dumps({
                        "run_id": run_id,
                        "jira_task_id": jira_task_id,
                        "repo": repo,
                    }),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "➕ Add Test Case"},
                    "action_id": "qa_add_test_case",
                    "value": json.dumps({
                        "run_id": run_id,
                        "jira_task_id": jira_task_id,
                        "repo": repo,
                    }),
                },
            ],
        }
    )

    return blocks


def post_test_cases(client: WebClient, channel: str, test_cases_path: str,
                    jira_task_id: str, run_id: str, repo: str, message_ts: str = None):
    with open(test_cases_path) as f:
        data = json.load(f)

    blocks = build_test_case_blocks(data, jira_task_id, run_id, repo)

    if message_ts:
        client.chat_update(
            channel=channel, ts=message_ts,
            blocks=blocks, text=f"Test Cases — {jira_task_id}"
        )
        print(f"Updated Slack message {message_ts}")
    else:
        resp = client.chat_postMessage(
            channel=channel, blocks=blocks, text=f"Test Cases — {jira_task_id}"
        )
        print(f"Posted to Slack: {resp['ts']}")


def ask_test_run(client: WebClient, channel: str, jira_task_id: str,
                 run_id: str, message_ts: str, repo: str):
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ Approved test cases for *{jira_task_id}* pushed to Zephyr.\n"
                    f"Would you like to create a *test run* now?"
                ),
            },
        },
        {
            "type": "actions",
            "block_id": f"qa_testrun_{run_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚀 Yes, Create Test Run"},
                    "style": "primary",
                    "action_id": "qa_create_test_run",
                    "value": json.dumps({"run_id": run_id, "jira_task_id": jira_task_id, "repo": repo}),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "⏭️ Skip for Now"},
                    "action_id": "qa_skip_test_run",
                    "value": json.dumps({"run_id": run_id, "jira_task_id": jira_task_id, "repo": repo}),
                },
            ],
        },
    ]
    client.chat_postMessage(
        channel=channel, thread_ts=message_ts, blocks=blocks,
        text=f"Create test run for {jira_task_id}?"
    )
    print("Asked about test run in thread.")


def simple_update(client: WebClient, channel: str, message_ts: str, text: str):
    client.chat_postMessage(channel=channel, thread_ts=message_ts, text=text)
    print(f"Posted update: {text}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="review",
                        choices=["review", "ask-test-run", "test-run-created", "test-run-skipped"])
    parser.add_argument("--test-cases", help="Path to test_cases.json")
    parser.add_argument("--jira-task", required=True)
    parser.add_argument("--run-id", help="GitHub Actions run ID")
    parser.add_argument("--message-ts", help="Slack message timestamp")
    parser.add_argument("--repo", help="GitHub repo (owner/name)")
    args = parser.parse_args()

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    channel = os.environ["SLACK_CHANNEL_ID"]

    if args.mode == "review":
        post_test_cases(client, channel, args.test_cases, args.jira_task,
                        args.run_id, args.repo, args.message_ts)

    elif args.mode == "ask-test-run":
        ask_test_run(client, channel, args.jira_task, args.run_id, args.message_ts, args.repo)

    elif args.mode == "test-run-created":
        simple_update(client, channel, args.message_ts,
                      f"🚀 Test run created in Zephyr for *{args.jira_task}*. Time to test!")

    elif args.mode == "test-run-skipped":
        simple_update(client, channel, args.message_ts,
                      f"⏭️ Test run skipped for *{args.jira_task}*. You can create it manually in Zephyr.")


if __name__ == "__main__":
    main()
