#!/usr/bin/env python3
"""
Detect flaky tests from kumahq/kuma master branch CI failures.
Creates/updates tracking issues on this repository.

Runs every 30 minutes via GitHub Actions. Uses a 2-hour lookback
window (with overlap) to avoid missing failures between runs.

Uses urllib for upstream (public) API reads and gh CLI for local
issue operations, since the GITHUB_TOKEN is scoped to this repo.
"""

import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
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

API_BASE = "https://api.github.com"


def upstream_api(path, params=None):
    """GET request to the GitHub REST API for public repos.

    Uses GH_TOKEN if available for higher rate limits (5000/h vs 60/h).
    """
    url = f"{API_BASE}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)

    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GH_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        print(f"  API error: {e.code} {e.reason} for {path}", file=sys.stderr)
        return None


def gh(*args):
    """Run gh CLI command (for local repo operations) and return stdout."""
    result = subprocess.run(
        ["gh"] + list(args),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"  gh error: {result.stderr.strip()}", file=sys.stderr)
        return ""
    return result.stdout.strip()


def get_master_runs(since, status=None, workflow=None):
    """Get workflow runs on master in the lookback window."""
    path = f"repos/{UPSTREAM}/actions/runs"
    if workflow:
        path = f"repos/{UPSTREAM}/actions/workflows/{workflow}/runs"
    params = {"branch": "master", "per_page": "30"}
    if status:
        params["status"] = status
    data = upstream_api(path, params)
    if not data:
        return []

    runs = data.get("workflow_runs", [])
    return [r for r in runs if r.get("created_at", "") >= since]


def build_job_success_timeline(all_runs):
    """For each run, find which jobs succeeded.

    A workflow run can be 'failure' overall but still have most jobs green.
    We care about individual job results, not the run-level conclusion.
    """
    result = []
    for run in all_runs:
        data = upstream_api(
            f"repos/{UPSTREAM}/actions/runs/{run['id']}/jobs",
            {"per_page": "100"},
        )
        if not data:
            print(f"  Timeline: no data for run {run['id']}")
            continue
        names = {
            j["name"] for j in data.get("jobs", [])
            if j.get("conclusion") == "success"
        }
        if names:
            result.append({"time": run["created_at"], "jobs": names})

    all_jobs = set()
    for entry in result:
        all_jobs.update(entry["jobs"])
    print(f"  Timeline: {len(result)} runs with successes, {len(all_jobs)} unique job names")
    for name in sorted(all_jobs):
        print(f"    - {name}")
    return result


def is_flaky(job_name, failure_time, job_successes):
    """A job is flaky if it also passed in a run AFTER the failure.

    passed, passed, failed, passed = flaky (passed after failure)
    passed, passed, failed, failed = probably broken (never passed after)
    """
    for entry in job_successes:
        if entry["time"] > failure_time and job_name in entry["jobs"]:
            return True
    return False


def get_failed_test_jobs(run_id):
    """Get failed jobs, excluding known non-test jobs."""
    data = upstream_api(
        f"repos/{UPSTREAM}/actions/runs/{run_id}/jobs",
        {"per_page": "100"},
    )
    if not data:
        return []

    jobs = data.get("jobs", [])
    failed = [j for j in jobs if j.get("conclusion") == "failure"]
    return [
        j for j in failed
        if not any(p in j.get("name", "").lower() for p in SKIP_JOB_PATTERNS)
    ]


def get_annotations(job_id):
    """Get failure annotations for a job (check run)."""
    data = upstream_api(f"repos/{UPSTREAM}/check-runs/{job_id}/annotations")
    if not data:
        return []

    annotations = [a for a in data if a.get("annotation_level") == "failure"]
    # Skip annotations from workflow files (not actual test failures)
    return [
        a for a in annotations
        if not a.get("path", "").startswith(".github")
    ]


def make_test_key(annotation):
    """Create a stable, readable identifier for a test from its annotation.

    Ginkgo annotations have minimal messages like "BeforeAll 03/19/26 00:03:36.42"
    and paths like "github.com/kumahq/kuma/v2/test/e2e_env/kubernetes/meshidentity/spire.go".
    We strip timestamps and extract a short, stable path.
    """
    path = annotation.get("path", "")
    message = annotation.get("message", "").strip()
    line = annotation.get("start_line", "")

    # Strip Ginkgo timestamps: "BeforeAll 03/19/26 00:03:36.42" -> "BeforeAll"
    desc = re.sub(r"\s+\d{2}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+", "", message)
    desc = desc.split("\n")[0][:120]

    if path:
        # Extract short path: ".../test/e2e_env/kubernetes/meshidentity/spire.go"
        # -> "kubernetes/meshidentity/spire"
        for prefix in ["test/e2e_env/", "test/e2e/", "test/"]:
            idx = path.find(prefix)
            if idx >= 0:
                path = path[idx + len(prefix):]
                break
        else:
            # Fallback: just use filename
            path = path.split("/")[-1]

        path = path.replace("_test.go", "").replace(".go", "")

        return f"{path} {desc}"

    return desc if desc else "unknown"


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
    if issue_number < 0:
        return False
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


def create_issue(test_key, occurrences):
    """Create a new flaky test tracking issue with all occurrences listed."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    first = occurrences[0]
    path = first.get("path", "unknown")
    line = first.get("line", "?")
    message = first.get("message", "No details")

    runs_list = "\n".join(
        f"- {o['run_time']} [{o['job_name']}]({o['run_url']})"
        for o in occurrences
    )

    body = (
        "## Flaky Test Detected\n\n"
        f"**Test:** `{test_key}`\n"
        f"**File:** `{path}:{line}`\n"
        f"**Occurrences:** {len(occurrences)}\n"
        f"**First seen:** {now}\n\n"
        f"### Flaky Runs\n\n{runs_list}\n\n"
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

    result = gh(
        "issue", "create",
        "--title", f"Flaky: {test_key}",
        "--label", LABEL,
        "--body", body,
    )
    if result:
        print(f"  Created issue: Flaky: {test_key} ({len(occurrences)} runs)")
        return True
    print(f"  Failed to create issue: Flaky: {test_key}")
    return False


def comment_on_issue(issue_number, new_runs):
    """Add new occurrences as a comment to an existing issue."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    runs_list = "\n".join(
        f"- {o['run_time']} [{o['job_name']}]({o['run_url']})"
        for o in new_runs
    )
    body = f"Flaked again ({len(new_runs)} new run(s) at {now}):\n{runs_list}"

    gh("issue", "comment", str(issue_number), "--body", body)
    print(f"  Commented on issue #{issue_number} ({len(new_runs)} new runs)")


def main():
    print(f"Checking {UPSTREAM} master for flaky tests (last {LOOKBACK_HOURS}h)...")

    since = (
        datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Focus on build-test-distribute which runs the actual tests
    wf = "build-test-distribute.yaml"

    failed_runs = get_master_runs(since, status="failure", workflow=wf)
    print(f"Found {len(failed_runs)} failed build-test-distribute run(s)")
    if not failed_runs:
        return

    # Get completed runs from the same workflow for the success timeline
    all_runs = get_master_runs(since, status="completed", workflow=wf)
    print(f"Found {len(all_runs)} completed run(s) for timeline")

    print("Building job success timeline...")
    job_successes = build_job_success_timeline(all_runs)

    # Phase 1: Collect all flaky failures grouped by test key
    print("\nCollecting flaky failures...")
    flaky_map = {}  # test_key -> [occurrences]
    skipped = 0

    for run in failed_runs:
        run_id = run["id"]
        run_url = run["html_url"]
        run_time = run["created_at"]

        failed_jobs = get_failed_test_jobs(run_id)

        for job in failed_jobs:
            job_id = job["id"]
            job_name = job["name"]

            if not is_flaky(job_name, run_time, job_successes):
                skipped += 1
                continue

            annotations = get_annotations(job_id)

            if annotations:
                for ann in annotations:
                    test_key = make_test_key(ann)
                    occurrence = {
                        "run_url": run_url,
                        "run_time": run_time,
                        "job_name": job_name,
                        "path": ann.get("path", "unknown"),
                        "line": ann.get("start_line", "?"),
                        "message": ann.get("message", ""),
                    }
                    flaky_map.setdefault(test_key, []).append(occurrence)
            else:
                test_key = f"job: {job_name}"
                occurrence = {
                    "run_url": run_url,
                    "run_time": run_time,
                    "job_name": job_name,
                    "path": "unknown",
                    "line": "?",
                    "message": "No annotations available. Check run logs.",
                }
                flaky_map.setdefault(test_key, []).append(occurrence)

    print(f"Found {len(flaky_map)} unique flaky test(s), skipped {skipped} non-flaky")

    # Phase 2: Create or update issues
    issues = get_open_flaky_issues()
    print(f"Tracking {len(issues)} existing flaky test issue(s)")

    created = 0

    for test_key, occurrences in flaky_map.items():
        existing = find_matching_issue(issues, test_key)

        if existing and existing["number"] > 0:
            # Filter to only new (untracked) runs
            new_runs = [
                o for o in occurrences
                if not is_run_tracked(existing["number"], o["run_url"])
            ]
            if new_runs:
                comment_on_issue(existing["number"], new_runs)
        elif not existing and created < MAX_ISSUES_PER_RUN:
            create_issue(test_key, occurrences)
            issues.append({
                "number": -1,
                "title": f"Flaky: {test_key}",
                "body": "",
            })
            created += 1

    print(f"\nDone. Created {created} new issue(s), skipped {skipped} non-flaky failure(s).")


if __name__ == "__main__":
    main()
