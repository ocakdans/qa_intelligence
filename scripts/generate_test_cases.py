"""
Calls Anthropic Claude API to generate structured test cases from Jira task
requirements. Outputs a JSON file consumed by subsequent workflow steps.

Robustness notes:
- Iterates content blocks to find text (some responses may put thinking or
  tool_use blocks first; we don't want to crash on content[0]).
- Strips ```json ... ``` fences if the model wraps its output despite the
  "no markdown fences" instruction.
- Emits a structured diagnostic to stderr if the response isn't parseable
  JSON: stop_reason, the shape of content blocks, and a preview of the raw
  text. That way GitHub Actions logs tell you exactly why the parse failed.
"""

import argparse
import json
import os
import re
import sys
import anthropic

SYSTEM_PROMPT = """You are a senior QA engineer. Given a Jira task ID and its requirements,
generate comprehensive test cases covering happy paths, edge cases, and negative scenarios.

Generate a MAXIMUM of 5 test cases. Prioritize the most important scenarios.

Return ONLY valid JSON — no markdown fences, no extra text — in this exact structure:
{
  "jira_task_id": "<task_id>",
  "test_cases": [
    {
      "id": 1,
      "title": "Short descriptive title",
      "preconditions": "Any required setup or state",
      "steps": [
        "Step 1 description",
        "Step 2 description"
      ],
      "expected_result": "What should happen",
      "type": "positive|negative|edge_case"
    }
  ]
}"""


# Matches ```json ... ``` or ``` ... ``` wrappers Claude occasionally adds.
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)


def _extract_text(content_blocks) -> str:
    """Concatenate every text-type content block in the response."""
    parts = []
    for block in content_blocks:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", "") or ""
            if text:
                parts.append(text)
    return "\n".join(parts).strip()


def _strip_code_fences(text: str) -> str:
    text = text.strip()
    m = _FENCE_RE.match(text)
    return m.group(1).strip() if m else text


def _describe_content_shape(content_blocks) -> list:
    """Compact summary of response content for the failure diagnostic."""
    shape = []
    for b in content_blocks:
        kind = getattr(b, "type", "?")
        entry = {"type": kind}
        if kind == "text":
            entry["text_len"] = len(getattr(b, "text", "") or "")
        elif kind == "thinking":
            entry["thinking_len"] = len(getattr(b, "thinking", "") or "")
        elif kind == "tool_use":
            entry["tool_name"] = getattr(b, "name", "?")
        shape.append(entry)
    return shape


def generate(jira_task_id: str, requirements: str) -> dict:
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=16000,
        # The system prompt is static — flag it for prompt caching. Note: on
        # Opus 4.5 the minimum cacheable prefix is 4096 tokens, so this short
        # (~300-token) prompt won't actually trigger a cache write today. The
        # marker is harmless and is ready for the day the prompt grows past
        # the floor (or the day we switch to a model with a smaller floor).
        system=[
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Jira Task: {jira_task_id}\n\n"
                    f"Requirements:\n{requirements}"
                ),
            }
        ],
    )

    raw_text = _extract_text(message.content)
    cleaned = _strip_code_fences(raw_text)

    if not cleaned:
        shape = _describe_content_shape(message.content)
        detail = (
            f"Claude returned no usable text. "
            f"stop_reason={message.stop_reason!r}, content_shape={shape}"
        )
        if message.stop_reason == "refusal":
            detail += f", stop_details={getattr(message, 'stop_details', None)!r}"
        print(detail, file=sys.stderr)
        raise RuntimeError(detail)

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        shape = _describe_content_shape(message.content)
        print(
            f"Claude response did not parse as JSON. "
            f"stop_reason={message.stop_reason!r}, content_shape={shape}, "
            f"parse_error={e}",
            file=sys.stderr,
        )
        preview = cleaned if len(cleaned) <= 2000 else cleaned[:2000] + "\n…[truncated]"
        print(f"Raw text:\n{preview}", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jira-task", required=True)
    parser.add_argument("--requirements", required=True)
    parser.add_argument("--output", default="test_cases.json")
    args = parser.parse_args()

    print(f"Generating test cases for {args.jira_task}...")
    result = generate(args.jira_task, args.requirements)

    with open(args.output, "w") as f:
        json.dump(result, f, indent=2)

    count = len(result.get("test_cases", []))
    print(f"Generated {count} test cases → {args.output}")


if __name__ == "__main__":
    main()
