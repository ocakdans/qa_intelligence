/**
 * Cloudflare Worker — Slack Interactivity Bridge
 *
 * Receives interactive payloads from Slack (button clicks, modal submissions)
 * and triggers the corresponding GitHub Actions workflow via repository_dispatch.
 *
 * Deploy with: wrangler deploy
 * Set secrets:  wrangler secret put SLACK_SIGNING_SECRET
 *               wrangler secret put GITHUB_TOKEN
 *               wrangler secret put GITHUB_REPO   (e.g. "ocakdans/qa_intelligence")
 */

const ACTION_TO_DISPATCH = {
  qa_approve_tc: "slack_approve_tc",
  qa_reject_tc: "slack_reject_tc",
  qa_push_to_zephyr: "slack_push_to_zephyr",
  qa_add_test_case: "slack_add_test_case",
  qa_create_test_run: "slack_create_test_run",
  qa_skip_test_run: "slack_skip_test_run",
};

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const body = await request.text();

    // Verify Slack signature
    const isValid = await verifySlackSignature(request, body, env.SLACK_SIGNING_SECRET);
    if (!isValid) {
      return new Response("Unauthorized", { status: 401 });
    }

    const params = new URLSearchParams(body);
    const payloadRaw = params.get("payload");
    if (!payloadRaw) {
      return new Response("Bad Request", { status: 400 });
    }

    const payload = JSON.parse(payloadRaw);

    // Handle block_actions (button clicks)
    if (payload.type === "block_actions") {
      const action = payload.actions?.[0];
      if (!action) return new Response("OK");

      const actionId = action.action_id;
      const dispatchType = ACTION_TO_DISPATCH[actionId];
      if (!dispatchType) return new Response("OK");

      const value = JSON.parse(action.value || "{}");
      const messageTs = payload.message?.ts || payload.container?.message_ts;
      const channelId = payload.channel?.id || payload.container?.channel_id;

      if (dispatchType === "slack_add_test_case") {
        // Open a modal for the user to fill in the test case
        await openAddTestCaseModal(payload.trigger_id, value, messageTs, channelId, env);
        return new Response("", { status: 200 });
      }

      await triggerGitHubDispatch(dispatchType, {
        ...value,
        message_ts: messageTs,
        channel_id: channelId,
      }, env);

      return new Response("", { status: 200 });
    }

    // Handle view_submission (modal submit — Add Test Case)
    if (payload.type === "view_submission") {
      const meta = JSON.parse(payload.view.private_metadata || "{}");
      const values = payload.view.state.values;

      const title = values.tc_title?.tc_title_input?.value || "";
      const steps = values.tc_steps?.tc_steps_input?.value || "";
      const expected = values.tc_expected?.tc_expected_input?.value || "";

      await triggerGitHubDispatch("slack_add_test_case", {
        run_id: meta.run_id,
        jira_task_id: meta.jira_task_id,
        repo: meta.repo,
        message_ts: meta.message_ts,
        channel_id: meta.channel_id,
        title,
        steps,
        expected,
      }, env);

      // Close modal
      return new Response(JSON.stringify({ response_action: "clear" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response("OK");
  },
};

async function triggerGitHubDispatch(eventType, clientPayload, env) {
  const url = `https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "QA-Intelligence-SlackHandler/1.0",
    },
    body: JSON.stringify({ event_type: eventType, client_payload: clientPayload }),
  });

  if (!resp.ok) {
    const text = await resp.text();
    console.error(`GitHub dispatch failed: ${resp.status} ${text}`);
  }
}

async function openAddTestCaseModal(triggerId, value, messageTs, channelId, env) {
  const modal = {
    type: "modal",
    title: { type: "plain_text", text: "Add Test Case" },
    submit: { type: "plain_text", text: "Add" },
    close: { type: "plain_text", text: "Cancel" },
    private_metadata: JSON.stringify({
      run_id: value.run_id,
      jira_task_id: value.jira_task_id,
      repo: value.repo,
      message_ts: messageTs,
      channel_id: channelId,
    }),
    blocks: [
      {
        type: "input",
        block_id: "tc_title",
        label: { type: "plain_text", text: "Test Case Title" },
        element: {
          type: "plain_text_input",
          action_id: "tc_title_input",
          placeholder: { type: "plain_text", text: "e.g. User can log in with valid credentials" },
        },
      },
      {
        type: "input",
        block_id: "tc_steps",
        label: { type: "plain_text", text: "Steps (separate with ' | ')" },
        element: {
          type: "plain_text_input",
          action_id: "tc_steps_input",
          multiline: true,
          placeholder: { type: "plain_text", text: "Navigate to login page | Enter email | Enter password | Click Login" },
        },
      },
      {
        type: "input",
        block_id: "tc_expected",
        label: { type: "plain_text", text: "Expected Result" },
        element: {
          type: "plain_text_input",
          action_id: "tc_expected_input",
          placeholder: { type: "plain_text", text: "User is redirected to the dashboard" },
        },
      },
    ],
  };

  await fetch("https://slack.com/api/views.open", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.SLACK_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ trigger_id: triggerId, view: modal }),
  });
}

async function verifySlackSignature(request, body, signingSecret) {
  const timestamp = request.headers.get("x-slack-request-timestamp");
  const slackSig = request.headers.get("x-slack-signature");
  if (!timestamp || !slackSig) return false;

  // Reject requests older than 5 minutes
  const now = Math.floor(Date.now() / 1000);
  if (Math.abs(now - parseInt(timestamp)) > 300) return false;

  const baseString = `v0:${timestamp}:${body}`;
  const encoder = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", encoder.encode(signingSecret),
    { name: "HMAC", hash: "SHA-256" },
    false, ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, encoder.encode(baseString));
  const computed = "v0=" + Array.from(new Uint8Array(sig))
    .map(b => b.toString(16).padStart(2, "0")).join("");

  // Constant-time comparison
  if (computed.length !== slackSig.length) return false;
  let diff = 0;
  for (let i = 0; i < computed.length; i++) {
    diff |= computed.charCodeAt(i) ^ slackSig.charCodeAt(i);
  }
  return diff === 0;
}
