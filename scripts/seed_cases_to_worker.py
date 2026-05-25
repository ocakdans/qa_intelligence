"""
POSTs the generated test cases to the Cloudflare Worker's /seed-cases
endpoint so the Worker can render Slack Approve/Reject repaints directly
(skipping the ~15-20s GitHub Actions cold-start round trip).

Soft-fails on any network or auth error — the Worker's slow path
(dispatching back to GitHub Actions) still works as a fallback, so a
seed-call failure shouldn't fail the whole generate workflow.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--test-cases", required=True, help="Path to test_cases.json")
    parser.add_argument("--run-id", required=True, help="GitHub Actions run ID (KV cache key)")
    args = parser.parse_args()

    worker_url = os.environ.get("WORKER_URL", "").rstrip("/")
    secret = os.environ.get("INTERNAL_WEBHOOK_SECRET", "")
    if not worker_url or not secret:
        print("WORKER_URL or INTERNAL_WEBHOOK_SECRET not set — skipping seed.",
              file=sys.stderr)
        return  # soft-fail; Worker will fall back to GitHub Actions dispatch

    with open(args.test_cases) as f:
        data = json.load(f)
    cases = data.get("test_cases") or []

    body = json.dumps({"run_id": args.run_id, "cases": cases}).encode()
    req = urllib.request.Request(
        f"{worker_url}/seed-cases",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Secret": secret,
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            status = resp.status
            body_text = resp.read().decode(errors="replace")
            print(f"Worker seed response: HTTP {status}")
            print(body_text)
    except urllib.error.HTTPError as e:
        print(f"Worker seed failed: HTTP {e.code} — {e.read().decode(errors='replace')}",
              file=sys.stderr)
    except Exception as e:
        print(f"Worker seed failed: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
