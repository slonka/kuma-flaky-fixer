#!/usr/bin/env python3
"""
Detect flaky tests from kumahq/kuma master branch CI failures.
Creates/updates tracking issues on this fork repository.

Runs every 30 minutes via GitHub Actions. Uses a 2-hour lookback
window (with overlap) to avoid missing failures between runs.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

UPSTREAM = os.environ.get("UPSTREAM_REPO", "kumahq/kuma")
LABEL = os.environ.get("LABEL", "flaky-test")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "2"))
MAX_ISSUES_PER_RUN = int(os.environ.get("MAX_ISSUES_PER_RUN", "10"))

# Job name patterns to skip (non-test jobs)
SKIP_JOB_PATTERNS = [
    "check", "build", "publish", "distribution", "merge",
    "docker", "release", "deploy", "create-", "scorecard",
]


def gh(*args):
    """Run gh CLI command and return stdout, or empty string on error."""
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  gh error: {result.stderr.strip()}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def get_failed_master_runs():
    """Get all failed workflow runs on master in the lookback window."""
    since = (
        datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    data = gh(
        "api",
        f"repos/{UPSTREAM}/actions/runs",
        "-f", "branch=master",
        "-f", "status=failure",
        "-f", "per_page=30",
        "--jq", ".workflow_runs",
    )
    if not data:
        return []

    runs = json.loads(data)
    return [r for r in runs if r.get("created_at", "") >= since]


def get_failed_test_jobs(run_id):
    """Get failed jobs, excluding known non-test jobs."""
    data = gh(
        "api",
        f"repos/{UPSTREAM}/actions/runs/{run_id}/jobs",
        "--paginate",
        "--jq", '.jobs | map(select(.conclusion == "failure"))',
    )
    if not data:
        return []

    jobs = json.loads(data)
    return [
        j for j in jobs
        if not any(p in j.get("name", "").lower() for p in SKIP_JOB_PATTERNS)
    ]


def get_annotations(job_id):
    """Get failure annotations for a job (check run)."""
    data = gh(
        "api",
        f"repos/{UPSTREAM}/check-runs/{job_id}/annotations",
        "--jq", '[.[] | select(.annotation_level == "failure")]',
    )
    if not data:
        return []
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return []


def make_test_key(annotation):
    """Create a stable, readable identifier for a test from its annotation."""
    path = annotation.get("path", "")
    message = annotation.get("message", "")

    # Extract first meaningful line as test description
    desc = ""
    for line in message.split("\n"):
        line = line.strip()
        if line and not line.startswith("[") and len(line) > 10:
            desc = line[:120]
            break

    if not desc:
        first_line = message.split("\n")[0] if message else "unknown"
        desc = first_line[:120]

    if path:
        short = path.split("/")[-1].replace("_test.go", "").replace(".go", "")
        return f"{short}: {desc}"

    return desc


def get_open_flaky_issues():
    """Get all open flaky-test issues from this repo."""
    data = gh(
        "issue", "list",
        "--label", LABEL,
        "--state", "open",
        "--limit", "100",
        "--json", "number,title,body",
    )
    if not data:
        return []
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return []


def find_matching_issue(issues, test_key):
    """Find an existing issue that matches this test key."""
    for issue in issues:
        if test_key in issue.get("title", ""):
            return issue
    return None


def is_run_tracked(issue_number, run_url):
    """Check if a run URL is already mentioned in an issue."""
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if not repo:
        return False

    # Check issue body
    data = gh(
        "api", f"repos/{repo}/issues/{issue_number}",
        "--jq", ".body",
    )
    if data and run_url in data:
        return True

    # Check comments
    data = gh(
        "api", f"repos/{repo}/issues/{issue_number}/comments",
        "--jq", ".[].body",
    )
    if data and run_url in data:
        return True

    return False


def create_issue(test_key, job_name, run_url, annotation):
    """Create a new flaky test tracking issue."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    path = annotation.get("path", "unknown")
    line = annotation.get("start_line", "?")
    message = annotation.get("message", "No details")

    body = (
        "## Flaky Test Detected\n\n"
        f"**Test:** `{test_key}`\n"
        f"**File:** `{path}:{line}`\n"
        f"**Job:** `{job_name}`\n"
        f"**First seen:** {now}\n"
        f"**Run:** {run_url}\n\n"
        "### Error\n"
        f"```\n{message[:1500]}\n```\n\n"
        "### Context\n\n"
        f"This test failed on the `master` branch of `{UPSTREAM}`.\n"
        "Master should always be green, so failures indicate flaky tests.\n\n"
        "### How to Fix\n\n"
        "- Look at the test file and understand what it's testing\n"
        "- Common causes: missing `Eventually()`, short timeouts, "
        "shared state, race conditions\n"
        "- See `.github/instructions/e2e-testing.instructions.md` for patterns\n"
        "- Add the PR label `ci/verify-stability` to prove the fix is stable\n"
    )

    gh(
        "issue", "create",
        "--title", f"Flaky: {test_key}",
        "--label", LABEL,
        "--body", body,
    )
    print(f"  Created issue: Flaky: {test_key}")


def create_job_level_issue(job_name, run_url):
    """Create an issue when we only have job-level failure info."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    test_key = f"job: {job_name}"

    body = (
        "## Flaky Test Job Detected\n\n"
        f"**Job:** `{job_name}`\n"
        f"**First seen:** {now}\n"
        f"**Run:** {run_url}\n\n"
        "### Context\n\n"
        f"This test job failed on the `master` branch of `{UPSTREAM}`.\n"
        "No specific test annotations were available.\n"
        "Check the run logs for details.\n"
    )

    gh(
        "issue", "create",
        "--title", f"Flaky: {test_key}",
        "--label", LABEL,
        "--body", body,
    )
    print(f"  Created job-level issue: {test_key}")


def comment_on_issue(issue_number, run_url, details=""):
    """Add an occurrence comment to an existing issue."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    body = f"Flaked again at {now}\nRun: {run_url}"
    if details:
        body += f"\n```\n{details[:500]}\n```"

    gh("issue", "comment", str(issue_number), "--body", body)
    print(f"  Commented on issue #{issue_number}")


def main():
    print(f"Checking {UPSTREAM} master for flaky tests (last {LOOKBACK_HOURS}h)...")

    runs = get_failed_master_runs()
    print(f"Found {len(runs)} failed master run(s)")
    if not runs:
        return

    issues = get_open_flaky_issues()
    print(f"Tracking {len(issues)} existing flaky test issue(s)")

    created = 0

    for run in runs:
        run_id = run["id"]
        run_url = run["html_url"]
        print(f"\nRun {run_id}: {run_url}")

        failed_jobs = get_failed_test_jobs(run_id)
        print(f"  {len(failed_jobs)} failed test job(s)")

        for job in failed_jobs:
            job_id = job["id"]
            job_name = job["name"]

            annotations = get_annotations(job_id)

            if annotations:
                for ann in annotations:
                    test_key = make_test_key(ann)
                    existing = find_matching_issue(issues, test_key)

                    if existing:
                        if not is_run_tracked(existing["number"], run_url):
                            comment_on_issue(
                                existing["number"],
                                run_url,
                                ann.get("message", "")[:500],
                            )
                    else:
                        if created < MAX_ISSUES_PER_RUN:
                            create_issue(test_key, job_name, run_url, ann)
                            issues.append({
                                "number": -1,
                                "title": f"Flaky: {test_key}",
                                "body": "",
                            })
                            created += 1
            else:
                test_key = f"job: {job_name}"
                existing = find_matching_issue(issues, test_key)

                if existing:
                    if not is_run_tracked(existing["number"], run_url):
                        comment_on_issue(existing["number"], run_url)
                else:
                    if created < MAX_ISSUES_PER_RUN:
                        create_job_level_issue(job_name, run_url)
                        issues.append({
                            "number": -1,
                            "title": f"Flaky: {test_key}",
                            "body": "",
                        })
                        created += 1

    print(f"\nDone. Created {created} new issue(s).")


if __name__ == "__main__":
    main()
