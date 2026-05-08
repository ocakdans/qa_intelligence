"""
Handles all Slack messaging for the QA pipeline:
  - Initial test cases review message (per-TC Approve / Reject buttons)
  - Ask-test-run prompt (after push to Qase)
  - Status updates: rejected, test-run-created, test-run-skipped
"""

import argparse
import json
import os
from slack_sdk import WebClient

JIRA_BASE_URL = "https://selimocakdan.atlassian.net"


def _parse_id_list(raw):
    """Parse a JSON array of IDs from the workflow payload.

    `toJson()` in GitHub Actions emits the literal string "null" when the
    field is absent, so guard for that and for malformed JSON. Returns None
    when no override was provided so the caller can fall back to the
    artifact contents.
    """
    if raw is None or raw == "" or raw.strip().lower() == "null":
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, list):
        return None
    return set(parsed)


def build_test_case_blocks(data: dict, jira_task_id: str, run_id: str, repo: str,
                           approved_ids: set = None, rejected_ids: set = None) -> list:
    test_cases = data.get("test_cases", [])
    # CLI/payload overrides win; the artifact JSON is just the fallback for
    # backwards compatibility.
    if approved_ids is None:
        approved_ids = set(data.get("approved_ids", []))
    if rejected_ids is None:
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
        type_emoji = {"positive": "🟢", "negative": "🔴", "edge_case": "⚠️"}.get(tc["type"], "•")
        steps_text = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(tc["steps"]))

        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"{type_emoji} *TC-{tc['id']}: {tc['title']}*\n"
                        f"*Preconditions:* {tc.get('preconditions', 'None')}\n"
                        f"*Steps:*\n{steps_text}\n"
                        f"*Expected:* {tc['expected_result']}"
                    ),
                },
            }
        )

        if is_approved:
            # Replace buttons with a clear approved indicator
            blocks.append({
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "✅ *Approved* — will be pushed to Qase"}],
            })
        else:
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
                                # Carry accumulated approved_ids so each click builds on previous
                                "approved_ids": list(approved_ids),
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
                                "approved_ids": list(approved_ids),
                            }),
                        },
                    ],
                }
            )

        blocks.append({"type": "divider"})

    # Bottom action bar — embed approved_ids in the push button so they survive cross-run
    blocks.append(
        {
            "type": "actions",
            "block_id": f"qa_bottom_{run_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚀 Push Approved to Qase"},
                    "style": "primary",
                    "action_id": "qa_push_to_qase",
                    "value": json.dumps({
                        "run_id": run_id,
                        "jira_task_id": jira_task_id,
                        "repo": repo,
                        "approved_ids": list(approved_ids),  # carry state in the button
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
                    jira_task_id: str, run_id: str, repo: str, message_ts: str = None,
                    approved_ids: set = None, rejected_ids: set = None):
    with open(test_cases_path) as f:
        data = json.load(f)

    blocks = build_test_case_blocks(data, jira_task_id, run_id, repo,
                                    approved_ids=approved_ids, rejected_ids=rejected_ids)

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
                    f"✅ Approved test cases for *{jira_task_id}* pushed to Qase.\n"
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


def notify_test_run_created(client: WebClient, channel: str, message_ts: str,
                            jira_task_id: str, run_id: str, repo: str):
    """Confirm a Qase run was created and offer to post a report back to Jira."""
    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"🚀 Test run created in Qase for *{jira_task_id}*. Time to test!",
            },
        },
        {
            "type": "actions",
            "block_id": f"qa_jira_report_{run_id}",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📊 Post Report to Jira"},
                    "style": "primary",
                    "action_id": "qa_post_jira_report",
                    "value": json.dumps({
                        "run_id": run_id,
                        "jira_task_id": jira_task_id,
                        "repo": repo,
                    }),
                }
            ],
        },
    ]
    client.chat_postMessage(
        channel=channel, thread_ts=message_ts, blocks=blocks,
        text=f"Test run created for {jira_task_id}",
    )
    print("Posted test-run-created notification with Jira report button.")


def notify_tc_action(client: WebClient, channel: str, message_ts: str,
                     action: str, tc_id: int, tc_title: str):
    """Post a brief thread reply confirming approve/reject of a single TC."""
    if action == "approve":
        text = f"✅ *TC-{tc_id}: {tc_title}* — Approved"
    else:
        text = f"❌ *TC-{tc_id}: {tc_title}* — Rejected"
    client.chat_postMessage(channel=channel, thread_ts=message_ts, text=text)
    print(text)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="review",
                        choices=["review", "ask-test-run", "test-run-created",
                                 "test-run-skipped", "tc-approved", "tc-rejected",
                                 "jira-report-posted"])
    parser.add_argument("--test-cases", help="Path to test_cases.json")
    parser.add_argument("--jira-task", required=True)
    parser.add_argument("--run-id", help="GitHub Actions run ID")
    parser.add_argument("--message-ts", help="Slack message timestamp")
    parser.add_argument("--repo", help="GitHub repo (owner/name)")
    parser.add_argument("--tc-id", type=int, help="Test case ID for tc-approved/tc-rejected modes")
    parser.add_argument("--approved-ids",
                        help="JSON array of approved IDs (overrides the artifact). Source of truth in KV.")
    parser.add_argument("--rejected-ids",
                        help="JSON array of rejected IDs (overrides the artifact). Source of truth in KV.")
    args = parser.parse_args()

    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    channel = os.environ["SLACK_CHANNEL_ID"]

    approved_override = _parse_id_list(args.approved_ids)
    rejected_override = _parse_id_list(args.rejected_ids)

    if args.mode == "review":
        post_test_cases(client, channel, args.test_cases, args.jira_task,
                        args.run_id, args.repo, args.message_ts,
                        approved_ids=approved_override, rejected_ids=rejected_override)

    elif args.mode in ("tc-approved", "tc-rejected"):
        # Look up the TC title from the artifact
        tc_title = f"Test Case {args.tc_id}"
        if args.test_cases:
            import json as _json
            with open(args.test_cases) as f:
                data = _json.load(f)
            for tc in data.get("test_cases", []):
                if tc["id"] == args.tc_id:
                    tc_title = tc["title"]
                    break
        action = "approve" if args.mode == "tc-approved" else "reject"
        notify_tc_action(client, channel, args.message_ts, action, args.tc_id, tc_title)

    elif args.mode == "ask-test-run":
        ask_test_run(client, channel, args.jira_task, args.run_id, args.message_ts, args.repo)

    elif args.mode == "test-run-created":
        notify_test_run_created(client, channel, args.message_ts,
                                args.jira_task, args.run_id, args.repo)

    elif args.mode == "test-run-skipped":
        simple_update(client, channel, args.message_ts,
                      f"⏭️ Test run skipped for *{args.jira_task}*. You can create it manually in Qase.")

    elif args.mode == "jira-report-posted":
        jira_url = f"{JIRA_BASE_URL}/browse/{args.jira_task}"
        simple_update(client, channel, args.message_ts,
                      f"📊 Test report posted on <{jira_url}|{args.jira_task}>.")


if __name__ == "__main__":
    main()
