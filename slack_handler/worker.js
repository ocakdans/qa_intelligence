/**
 * Cloudflare Worker — Slack Interactivity Bridge
 *
 * Owns the per-run approval state in KV. Every Slack click reads the latest
 * state, applies the change atomically, persists, and only then dispatches to
 * GitHub Actions with the *full merged state* in client_payload. This kills
 * the artifact-cross-run race that was losing approvals when buttons were
 * clicked rapidly.
 *
 * KV layout: state:{run_id} → {
 *   approved_ids: number[],
 *   rejected_ids: number[],
 *   approved_by:  { [tc_id]: username },   // who approved each case
 *   rejected_by:  { [tc_id]: username },   // who rejected each case
 * }
 *
 * Deploy: wrangler deploy
 * Bindings:
 *   - QA_STATE          (KV namespace, declared in wrangler.toml)
 * Secrets (wrangler secret put):
 *   - SLACK_SIGNING_SECRET
 *   - SLACK_BOT_TOKEN
 *   - GITHUB_TOKEN
 *   - GITHUB_REPO   (e.g. "ocakdans/qa_intelligence")
 */

const ACTION_TO_DISPATCH = {
  qa_approve_tc: "slack_approve_tc",
  qa_reject_tc: "slack_reject_tc",
  qa_push_to_qase: "slack_push_to_qase",
  qa_add_test_case: "slack_add_test_case",
  qa_create_test_run: "slack_create_test_run",
  qa_skip_test_run: "slack_skip_test_run",
  qa_post_jira_report: "slack_post_jira_report",
  qa_create_bug_tickets: "slack_create_bug_tickets",
  qa_post_comment_only: "slack_post_comment_only",
};

const STATE_ACTIONS = new Set(["qa_approve_tc", "qa_reject_tc"]);

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const url = new URL(request.url);

    // ── Jira automation webhook ────────────────────────────────────
    // Fires when an issue transitions into the QA column. Auth is a
    // shared secret in the X-Webhook-Secret header.
    if (url.pathname === "/jira-webhook") {
      return handleJiraWebhook(request, env);
    }

    // ── Slack interactivity (default) ──────────────────────────────
    const body = await request.text();

    if (!(await verifySlackSignature(request, body, env.SLACK_SIGNING_SECRET))) {
      return new Response("Unauthorized", { status: 401 });
    }

    const params = new URLSearchParams(body);
    const payloadRaw = params.get("payload");
    if (!payloadRaw) {
      return new Response("Bad Request", { status: 400 });
    }

    const payload = JSON.parse(payloadRaw);

    // ── Button clicks ────────────────────────────────────────────────
    if (payload.type === "block_actions") {
      const action = payload.actions?.[0];
      if (!action) return new Response("OK");

      const actionId = action.action_id;
      const dispatchType = ACTION_TO_DISPATCH[actionId];
      if (!dispatchType) return new Response("OK");

      const value = JSON.parse(action.value || "{}");
      const messageTs = payload.message?.ts || payload.container?.message_ts;
      const channelId = payload.channel?.id || payload.container?.channel_id;

      // Add Test Case opens a modal — no state change yet, just include
      // current state so the modal submission can re-render correctly.
      if (dispatchType === "slack_add_test_case") {
        const state = await readState(env, value.run_id);
        await openAddTestCaseModal(payload.trigger_id, value, messageTs, channelId, state, env);
        return new Response("", { status: 200 });
      }

      let state;
      if (STATE_ACTIONS.has(actionId)) {
        // Approve/Reject — mutate state in KV before dispatching. Capture
        // who clicked so we can attribute the action in the Slack repaint.
        const clicker = pickUsername(payload.user);
        state = await applyClick(env, value.run_id, actionId, parseInt(value.tc_id, 10), clicker);
      } else {
        // Push / Create-test-run / Skip — read-only, take whatever's persisted.
        state = await readState(env, value.run_id);
      }

      await triggerGitHubDispatch(dispatchType, {
        ...value,
        message_ts: messageTs,
        channel_id: channelId,
        approved_ids: state.approved_ids,
        rejected_ids: state.rejected_ids,
        approved_by: state.approved_by,
        rejected_by: state.rejected_by,
      }, env);

      return new Response("", { status: 200 });
    }

    // ── Modal submit (Add Test Case) ─────────────────────────────────
    if (payload.type === "view_submission") {
      const meta = JSON.parse(payload.view.private_metadata || "{}");
      const values = payload.view.state.values;

      const title = values.tc_title?.tc_title_input?.value || "";
      const steps = values.tc_steps?.tc_steps_input?.value || "";
      const expected = values.tc_expected?.tc_expected_input?.value || "";

      const state = await readState(env, meta.run_id);

      await triggerGitHubDispatch("slack_add_test_case", {
        run_id: meta.run_id,
        jira_task_id: meta.jira_task_id,
        repo: meta.repo,
        message_ts: meta.message_ts,
        channel_id: meta.channel_id,
        title,
        steps,
        expected,
        approved_ids: state.approved_ids,
        rejected_ids: state.rejected_ids,
        approved_by: state.approved_by,
        rejected_by: state.rejected_by,
      }, env);

      return new Response(JSON.stringify({ response_action: "clear" }), {
        headers: { "Content-Type": "application/json" },
      });
    }

    return new Response("OK");
  },
};

// ── KV state helpers ──────────────────────────────────────────────────

const STATE_KEY = (runId) => `state:${runId}`;
const EMPTY_STATE = () => ({
  approved_ids: [],
  rejected_ids: [],
  approved_by: {},
  rejected_by: {},
});

async function readState(env, runId) {
  if (!runId) return EMPTY_STATE();
  const raw = await env.QA_STATE.get(STATE_KEY(runId));
  if (!raw) return EMPTY_STATE();
  try {
    const parsed = JSON.parse(raw);
    return {
      approved_ids: Array.isArray(parsed.approved_ids) ? parsed.approved_ids : [],
      rejected_ids: Array.isArray(parsed.rejected_ids) ? parsed.rejected_ids : [],
      // Backward-compat: older KV entries don't have the attribution maps.
      approved_by: isPlainObject(parsed.approved_by) ? parsed.approved_by : {},
      rejected_by: isPlainObject(parsed.rejected_by) ? parsed.rejected_by : {},
    };
  } catch {
    return EMPTY_STATE();
  }
}

function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

/**
 * Pull a human-readable handle off a Slack interactivity `user` object.
 * Prefer username, fall back to name, finally to id.
 */
function pickUsername(user) {
  if (!user) return "user";
  return user.username || user.name || user.id || "user";
}

/**
 * Atomically apply an Approve/Reject click to the KV state and return the
 * new state. KV doesn't support real CAS, so we minimize the read-write
 * window by doing nothing else between the read and the put.
 */
async function applyClick(env, runId, actionId, tcId, clicker) {
  const state = await readState(env, runId);
  const approved = new Set(state.approved_ids);
  const rejected = new Set(state.rejected_ids);
  const approvedBy = { ...state.approved_by };
  const rejectedBy = { ...state.rejected_by };

  const key = String(tcId);

  if (actionId === "qa_approve_tc") {
    approved.add(tcId);
    rejected.delete(tcId);
    approvedBy[key] = clicker || "user";
    delete rejectedBy[key];
  } else if (actionId === "qa_reject_tc") {
    rejected.add(tcId);
    approved.delete(tcId);
    rejectedBy[key] = clicker || "user";
    delete approvedBy[key];
  }

  const next = {
    approved_ids: [...approved].sort((a, b) => a - b),
    rejected_ids: [...rejected].sort((a, b) => a - b),
    approved_by: approvedBy,
    rejected_by: rejectedBy,
  };

  await env.QA_STATE.put(STATE_KEY(runId), JSON.stringify(next));
  return next;
}

// ── GitHub & Slack glue ───────────────────────────────────────────────

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

async function openAddTestCaseModal(triggerId, value, messageTs, channelId, state, env) {
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
      // We snapshot the state at modal-open time too, but the submission
      // re-reads from KV so the modal can sit open without going stale.
      approved_ids: state.approved_ids,
      rejected_ids: state.rejected_ids,
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

// ── Jira webhook handler ─────────────────────────────────────────────

async function handleJiraWebhook(request, env) {
  // Shared-secret auth. The same value must be set in the Jira automation
  // rule's "Send web request" action under the X-Webhook-Secret header.
  const supplied = request.headers.get("X-Webhook-Secret");
  if (!env.JIRA_WEBHOOK_SECRET || !supplied || !constantTimeEquals(supplied, env.JIRA_WEBHOOK_SECRET)) {
    return new Response("Unauthorized", { status: 401 });
  }

  let payload;
  try {
    payload = await request.json();
  } catch {
    return new Response("Bad Request: body must be JSON", { status: 400 });
  }

  // Accept either a flat shape (sent by Jira automation) or a nested issue
  // shape (sent by Jira's built-in webhooks).
  const issue = payload.issue || payload;
  const key = issue.key || payload.key;
  const summary = (issue.fields && issue.fields.summary) || issue.summary || payload.summary || "";
  const description = (issue.fields && issue.fields.description) || issue.description || payload.description || "";

  if (!key) {
    return new Response("Bad Request: missing issue key", { status: 400 });
  }

  const requirements = [summary, description].filter(Boolean).join("\n\n").trim();
  if (!requirements) {
    return new Response("Bad Request: issue has no summary or description to use as requirements", { status: 400 });
  }

  await triggerGitHubDispatch("jira_task_to_qa", {
    jira_task_id: key,
    requirements,
  }, env);

  return new Response(JSON.stringify({ ok: true, key }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function constantTimeEquals(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function verifySlackSignature(request, body, signingSecret) {
  const timestamp = request.headers.get("x-slack-request-timestamp");
  const slackSig = request.headers.get("x-slack-signature");
  if (!timestamp || !slackSig) return false;

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

  if (computed.length !== slackSig.length) return false;
  let diff = 0;
  for (let i = 0; i < computed.length; i++) {
    diff |= computed.charCodeAt(i) ^ slackSig.charCodeAt(i);
  }
  return diff === 0;
}
