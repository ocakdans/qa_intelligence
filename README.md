# QA Intelligence

An autonomous QA pipeline that turns a Jira ticket into test cases, runs them through human review in Slack, pushes the approved ones to Qase, executes a test run, and reports results back to Jira — including the option to auto-create bug tickets for failures.

```
Jira (move ticket to QA)
        │  webhook (Jira automation)
        ▼
Cloudflare Worker  ── shared secret ──▶  GitHub Actions
        │                                 │
        │                                 ▼
        │                          Claude API (Opus)
        │                                 │
        │                                 ▼
        ◀─────────────── Slack (review UI)
                                          │
                       Approve / Reject buttons (state in Cloudflare KV)
                                          │
                                          ▼
                                    Qase REST API
                                          │
                            Test cases → Test run → Results
                                          │
                                          ▼
                                  Jira comment + (optional) bug tickets
```

## Features

- **Auto-generate** up to 5 test cases per Jira ticket using Claude
- **Slack-native review** with per-test-case Approve / Reject buttons and a manual "Add Test Case" modal
- **Race-condition-free state** for approvals (Cloudflare KV is the single source of truth)
- **One Qase suite per Jira task** (re-runs append, no duplicates)
- **Stateless test reporting** — always reflects the latest Qase execution
- **Bug-ticket fan-out** — one click creates a Jira bug per failed test case, with steps and expected results attached
- **Re-clickable everything** — every button can be pressed multiple times; each click is independent and fresh

## Prerequisites

You need accounts on:

- **GitHub** (host the repo + run GitHub Actions)
- **Slack** workspace (admin to create an app)
- **Cloudflare** (workers.dev subdomain is free)
- **Anthropic** (Claude API credits)
- **Qase** (free tier works)
- **Atlassian Jira Cloud**

Locally, install:

- Python 3.12+
- Node.js 20+
- [`gh`](https://cli.github.com/) (GitHub CLI)
- [`wrangler`](https://developers.cloudflare.com/workers/wrangler/install-and-update/) (`npm i -g wrangler`)

## Setup

### 1. Fork and clone

```bash
gh repo fork <this-repo> --clone
cd qa_intelligence
pip install -r requirements.txt
```

### 2. Anthropic API key

Create a key at https://console.anthropic.com/settings/keys. Top up enough credit for Opus (a single run uses ~3,000–5,000 tokens).

### 3. Slack app

1. Go to https://api.slack.com/apps → **Create New App** → **From scratch**
2. Name: "QA Intelligence", pick your workspace
3. Under **OAuth & Permissions** → **Bot Token Scopes**, add:
   - `chat:write`
   - `chat:write.public`
   - `commands`
   - `users:read`
   - `views:open`
4. Install the app to your workspace, copy the **Bot User OAuth Token** (`xoxb-…`)
5. Under **Basic Information** → copy the **Signing Secret**
6. Create a channel (e.g. `#all-qa-intelligence`), invite the bot, copy the channel ID (right-click channel → Copy link → last segment)
7. **Don't set the Interactivity URL yet** — you'll set it after deploying the Worker in step 7

### 4. Qase

1. Sign up at https://qase.io
2. Create a project — note the **project code** (e.g. `QI`)
3. Profile → **API tokens** → generate one, copy it

### 5. Jira

1. Generate an API token at https://id.atlassian.com/manage-profile/security/api-tokens
2. Note your Atlassian email and tenant URL (e.g. `https://yourtenant.atlassian.net`)
3. Create a project (e.g. `QCT`). Make sure your workflow has a status called **`QA`**.

### 6. GitHub Actions secrets

In your forked repo: **Settings → Secrets and variables → Actions → New repository secret**. Add all of these:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | from step 2 |
| `SLACK_BOT_TOKEN` | from step 3 (xoxb-…) |
| `SLACK_CHANNEL_ID` | from step 3 (C…) |
| `QASE_API_TOKEN` | from step 4 |
| `QASE_PROJECT_CODE` | from step 4 (e.g. `QI`) |
| `JIRA_BASE_URL` | from step 5 (e.g. `https://yourtenant.atlassian.net`) |
| `JIRA_EMAIL` | from step 5 |
| `JIRA_API_TOKEN` | from step 5 |

### 7. Cloudflare Worker

The Worker is the live HTTP endpoint Slack and Jira call. It owns the approval state in Cloudflare KV.

```bash
cd slack_handler

# Login (one time)
wrangler login

# Create the KV namespace for approval state
wrangler kv namespace create QA_STATE
# Copy the printed `id` value
```

Open `slack_handler/wrangler.toml` and paste the KV namespace ID:

```toml
[[kv_namespaces]]
binding = "QA_STATE"
id = "PASTE_THE_ID_FROM_THE_PREVIOUS_COMMAND"
```

Set Worker secrets (generate a fresh random secret for the Jira webhook):

```bash
# Use any 32+ char random string; openssl works:
JIRA_WEBHOOK_SECRET=$(openssl rand -hex 32)
echo "Save this somewhere — you'll paste it into Jira automation later:"
echo "$JIRA_WEBHOOK_SECRET"

wrangler secret put SLACK_SIGNING_SECRET   # paste from step 3
wrangler secret put SLACK_BOT_TOKEN        # paste from step 3
wrangler secret put GITHUB_TOKEN           # see note below
wrangler secret put GITHUB_REPO            # e.g. yourname/qa_intelligence
wrangler secret put JIRA_WEBHOOK_SECRET    # paste $JIRA_WEBHOOK_SECRET
```

For `GITHUB_TOKEN`: create a fine-grained Personal Access Token at https://github.com/settings/personal-access-tokens/new with **Contents: Read & Write** and **Actions: Write** on this repo only.

Deploy:

```bash
wrangler deploy
# Output ends with the Worker URL, e.g.
# https://qa-intelligence-slack-handler.<account>.workers.dev
```

### 8. Wire Slack to the Worker

Back in the Slack app config (https://api.slack.com/apps):

- **Interactivity & Shortcuts** → enable → **Request URL**: `https://<your-worker-url>/`
- Save

### 9. Wire Jira to the Worker

In Jira: **Project settings → Automation → Create flow** (a.k.a. Create rule).

1. **Trigger**: *Work item transitioned*
   - To status: **QA**
2. **Action**: *Send web request*
   - URL: `https://<your-worker-url>/jira-webhook`
   - HTTP method: `POST`
   - Headers:
     - `Content-Type` → `application/json`
     - `X-Webhook-Secret` → the `JIRA_WEBHOOK_SECRET` value you generated in step 7
   - Web request body: **Custom data**
   - Custom data:
     ```json
     {
       "key": {{issue.key.asJsonString}},
       "summary": {{issue.summary.asJsonString}},
       "description": {{issue.description.asJsonString}}
     }
     ```
     Note: no quotes around `{{...asJsonString}}` — the postfix includes the quotes and handles JSON-escaping for newlines/quotes inside the values.
3. Name the flow `Auto-generate QA test cases on QA transition` and **Turn it on**.

## Usage

### Happy path

1. **Create or pick a Jira ticket** in your project. Give it a clear description (this becomes the test-case requirements).
2. **Move the ticket to the QA column.** Within ~30 seconds, a "🧪 Test Cases — <KEY>" message appears in your Slack channel.
3. **Review each test case.** Click **Approve** or **Reject** on each one. Click as fast as you want — Cloudflare KV serializes the state, so nothing gets lost.
4. **Click "🚀 Push Approved to Qase."** The approved test cases are created in a Qase suite named after your Jira ticket (e.g. `QCT-1`). Existing suites are reused — no duplicates.
5. **A thread reply asks "Create test run now?"** Click **Yes** to create a Qase test run with the approved cases.
6. **Execute the tests in Qase.** Mark each case Passed/Failed/Blocked.
7. **Click "📊 Post Report to Jira"** in the Slack thread. A summary comment lands on the Jira ticket with counts and a per-case breakdown.
8. **If there were failures**, the same Slack message offers **"🐞 Create Bug Tickets"**. Clicking it creates one Jira bug per failed test case, linked back to the parent ticket, with steps and expected result included.

Every button is re-clickable — re-execute in Qase, click "Post Report" again, and you get a fresh comment with the latest state.

### Manual generation

You can also trigger generation manually without moving a ticket:

```bash
gh workflow run "Generate Test Cases" \
  -f jira_task_id=QCT-1 \
  -f requirements="As a user I want to log in with email + password..."
```

## Project structure

```
.
├── .github/workflows/
│   ├── generate_test_cases.yml   # Triggered by manual run or Jira webhook
│   └── handle_approval.yml       # Reacts to every Slack click via repository_dispatch
├── scripts/
│   ├── generate_test_cases.py    # Calls Claude, writes test_cases.json artifact
│   ├── post_to_slack.py          # Renders Block Kit messages, refreshes them
│   ├── push_to_qase.py           # Creates Qase suite + cases + test runs
│   ├── add_test_case.py          # Appends manual test cases from the Slack modal
│   └── report_to_jira.py         # Reads latest Qase run, posts Jira comment, creates bugs
├── slack_handler/
│   ├── worker.js                 # Cloudflare Worker: Slack + Jira webhooks → GitHub dispatch
│   └── wrangler.toml             # Worker config (KV binding, secrets list)
├── requirements.txt
├── .env.example
└── README.md
```

## How it works under the hood

### State lives in Cloudflare KV, not in artifacts

GitHub Actions Artifacts v4 binds uploads to the *uploading* run, not the source run. That made cross-run state (approve TC-1 in run A, approve TC-2 in run B, push in run C) lose data. The Worker now owns `state:{run_id}` in KV with `{approved_ids, rejected_ids}`, mutates it atomically per click, and threads the full state into the `client_payload` of every GitHub dispatch. The Action is stateless — it just renders Slack and pushes to Qase from the canonical payload.

### Why two GitHub workflows

- `generate_test_cases.yml` — fires on `workflow_dispatch` (manual) or `repository_dispatch: jira_task_to_qa` (from the Worker). Calls Claude, uploads the artifact, posts the initial Slack message.
- `handle_approval.yml` — fires on every other Slack interaction (`slack_approve_tc`, `slack_reject_tc`, `slack_push_to_qase`, `slack_add_test_case`, `slack_create_test_run`, `slack_skip_test_run`, `slack_post_jira_report`, `slack_create_bug_tickets`). A concurrency group on `run_id` serializes Slack repaints.

### Why "Post Report to Jira" always posts a fresh comment

Jira allows multiple comments on the same issue. Each click of the button calls `report_to_jira.py`, which queries Qase for the latest run of the suite matching the Jira task ID, picks the most recent result per case (dedup by `end_time`), formats wiki-markup, and POSTs to `/rest/api/2/issue/{key}/comment`. No artifact dependency — re-execute the tests, click again, get a brand-new comment.

## Troubleshooting

### Slack message has no "Push Approved to Qase" button

You're looking at an old message posted before the Worker was redeployed. Trigger a fresh generate (`gh workflow run "Generate Test Cases" …` or move a ticket to QA) and use the new message.

### "Bad Request: body must be JSON" in Jira audit log

Your Jira automation's Custom data isn't using `.asJsonString`. The fix is in step 9 above — the `{{...asJsonString}}` postfix is required because raw `{{issue.description}}` can contain unescaped newlines/quotes that break JSON.

### "Jira auth failed: HTTP 401"

`JIRA_API_TOKEN` is invalid or doesn't match `JIRA_EMAIL`. Regenerate at https://id.atlassian.com/manage-profile/security/api-tokens and update the GitHub secret with `gh secret set JIRA_API_TOKEN`.

### "Jira issue X not visible to JIRA_EMAIL"

Either the issue doesn't exist or the user behind the API token doesn't have Browse Project permission. Open `https://<your-tenant>.atlassian.net/browse/<KEY>` in a browser to verify.

### "No approved test cases to push"

The push button's payload had an empty `approved_ids` list. Approve at least one test case before clicking push. If you did and it still fails, the Worker may not be redeployed — run `cd slack_handler && wrangler deploy`.

### Claude generates empty or unparseable output

`scripts/generate_test_cases.py` logs `stop_reason` and a content-shape summary to stderr on parse failure. Check the GitHub Actions log. Common causes: `max_tokens` too low (bump it), Claude hit a safety refusal (`stop_reason='refusal'` — rephrase the ticket description), or the API returned a markdown-fenced response (the script strips fences automatically, but check the preview in the error).

## Cost estimate

For a typical generate-review-push-execute-report cycle on a single Jira ticket:

| Component | Cost |
|---|---|
| Claude Opus (1× generation + 1× bug-ticket batch) | ~$0.05–$0.15 |
| GitHub Actions (~6 short jobs, free tier on public repos) | $0 |
| Cloudflare Worker + KV (well within free tier) | $0 |
| Slack / Qase / Jira free tiers | $0 |

The Anthropic API spend is the only material cost.

## License

MIT. See `LICENSE`.
