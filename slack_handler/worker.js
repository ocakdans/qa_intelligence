/**
 * Cloudflare Worker — Slack Interactivity Bridge
 *
 * Owns the per-run approval state in KV. Every Slack click reads the latest
 * state, applies the change atomically, persists, and only then dispatches to
 * GitHub Actions with the *full merged state* in client_payload. This kills
 * the artifact-cross-run race that was losing approvals when buttons were
 * clicked rapidly.
 *
 * KV layout: state:{run_id} → { approved_ids: number[], rejected_ids: number[] }
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
};

const STATE_ACTIONS = new Set(["qa_approve_tc", "qa_reject_tc"]);

export default {
  async fetch(request, env) {
    if (request.method !== "POST") {
      return new Response("Method Not Allowed", { status: 405 });
    }

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
        // Approve/Reject — mutate state in KV before dispatching.
        state = await applyClick(env, value.run_id, actionId, parseInt(value.tc_id, 10));
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
const EMPTY_STATE = () => ({ approved_ids: [], rejected_ids: [] });

async function readState(env, runId) {
  if (!runId) return EMPTY_STATE();
  const raw = await env.QA_STATE.get(STATE_KEY(runId));
  if (!raw) return EMPTY_STATE();
  try {
    const parsed = JSON.parse(raw);
    return {
      approved_ids: Array.isArray(parsed.approved_ids) ? parsed.approved_ids : [],
      rejected_ids: Array.isArray(parsed.rejected_ids) ? parsed.rejected_ids : [],
    };
  } catch {
    return EMPTY_STATE();
  }
}

/**
 * Atomically apply an Approve/Reject click to the KV state and return the
 * new state. KV doesn't support real CAS, so we minimize the read-write
 * window by doing nothing else between the read and the put.
 */
async function applyClick(env, runId, actionId, tcId) {
  const state = await readState(env, runId);
  const approved = new Set(state.approved_ids);
  const rejected = new Set(state.rejected_ids);

  if (actionId === "qa_approve_tc") {
    approved.add(tcId);
    rejected.delete(tcId);
  } else if (actionId === "qa_reject_tc") {
    rejected.add(tcId);
    approved.delete(tcId);
  }

  const next = {
    approved_ids: [...approved].sort((a, b) => a - b),
    rejected_ids: [...rejected].sort((a, b) => a - b),
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
