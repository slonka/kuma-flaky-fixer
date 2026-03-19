#!/usr/bin/env python3
"""
Weekly triage: rank flaky tests by frequency and assign top ones to copilot.

Runs Monday 6 AM UTC. Picks the most frequently flaking tests
(by comment count) and assigns them to GitHub Copilot coding agent.

Requires a PAT with repo scope stored as COPILOT_PAT secret,
since the default GITHUB_TOKEN cannot assign the copilot agent.
"""

import json
import os
import subprocess
import sys

LABEL = os.environ.get("LABEL", "flaky-test")
MAX_ASSIGN = int(os.environ.get("MAX_ASSIGN", "5"))
MIN_OCCURRENCES = 2  # Need at least this many comments (reruns) to qualify


def gh(*args):
    """Run gh CLI command and return stdout."""
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  gh error: {result.stderr.strip()}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def assign_copilot(issue_number):
    """Assign the Copilot coding agent to an issue via REST API."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    body = json.dumps({
        "assignees": ["copilot-swe-agent[bot]"],
        "agent_assignment": {
            "target_repo": repo,
            "base_branch": "master",
        },
    })

    proc = subprocess.run(
        [
            "gh", "api", "--method", "POST",
            "-H", "Accept: application/vnd.github+json",
            f"repos/{repo}/issues/{issue_number}/assignees",
            "--input", "-",
        ],
        input=body,
        capture_output=True,
        text=True,
    )

    if proc.returncode != 0:
        print(f"  Failed to assign copilot to #{issue_number}: {proc.stderr.strip()}")
        return False

    response = json.loads(proc.stdout)
    assignees = [a.get("login") for a in response.get("assignees", [])]
    if "Copilot" in assignees:
        print(f"  Assigned copilot to #{issue_number}")
        return True

    print(f"  Assignment response missing Copilot: {assignees}")
    return False


def main():
    print("Triaging flaky test issues...")

    data = gh(
        "issue", "list",
        "--label", LABEL,
        "--state", "open",
        "--limit", "50",
        "--json", "number,title,comments,assignees",
    )
    if not data:
        print("No flaky test issues found.")
        return

    issues = json.loads(data)

    # Filter out already-assigned issues (Copilot shows as "Copilot" login)
    unassigned = [
        i for i in issues
        if not any(
            a.get("login") == "Copilot" for a in i.get("assignees", [])
        )
    ]

    if not unassigned:
        print("All flaky test issues are already assigned.")
        return

    # Sort by comment count descending (comments = additional occurrences)
    unassigned.sort(key=lambda i: i.get("comments", 0), reverse=True)

    # Only assign issues with enough occurrences
    candidates = [
        i for i in unassigned if i.get("comments", 0) >= MIN_OCCURRENCES
    ]

    if not candidates:
        print(
            f"No issues with >= {MIN_OCCURRENCES} additional occurrences. "
            "Waiting for more data."
        )
        return

    to_assign = candidates[:MAX_ASSIGN]

    print(f"Assigning top {len(to_assign)} flaky test(s) to copilot:\n")
    for issue in to_assign:
        num = issue["number"]
        title = issue["title"]
        occurrences = issue.get("comments", 0) + 1  # +1 for initial report

        print(f"  #{num}: {title} ({occurrences} occurrences)")
        assign_copilot(num)

    print("\nDone!")


if __name__ == "__main__":
    main()
