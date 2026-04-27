"""
Calls OpenAI API to generate structured test cases from Jira task requirements.
Outputs a JSON file consumed by subsequent workflow steps.
"""

import argparse
import json
import os
import sys
from openai import OpenAI

SYSTEM_PROMPT = """You are a senior QA engineer. Given a Jira task ID and its requirements,
generate comprehensive test cases covering happy paths, edge cases, and negative scenarios.

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


def generate(jira_task_id: str, requirements: str) -> dict:
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])

    response = client.chat.completions.create(
        model="gpt-4o",
        max_tokens=4096,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Jira Task: {jira_task_id}\n\n"
                    f"Requirements:\n{requirements}"
                ),
            },
        ],
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content.strip()
    return json.loads(raw)


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
