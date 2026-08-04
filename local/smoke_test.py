"""Smoke test proving the whole deploy loop works: push code -> CDK synth ->
deploy -> this script -> teardown.

Standard library only (urllib, json, argparse, time) so it runs from a clean
checkout, inside CI, with no install step.

Usage:
    python3 local/smoke_test.py \
        [--api-url URL]            # default http://localhost:8000
        [--frontend-url URL]       # optional; skipped if omitted
        [--expect-commit SHA]      # optional
        [--expect-environment ENV] # optional
        [--timeout SECONDS]        # default 180
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request


def _get(url: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _retry_get(url: str, overall_timeout: float) -> tuple[int, str]:
    """GETs `url`, retrying on connection error / non-2xx with a 5s backoff,
    up to `overall_timeout` seconds total. A freshly deployed API Gateway +
    a cold container Lambda (or a CloudFront distribution that just got
    created) can take a while to come up."""
    deadline = time.monotonic() + overall_timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            status, body = _get(url, timeout=10)
            if 200 <= status < 300:
                return status, body
            last_error = f"status {status}: {body[:500]}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = str(exc)
        time.sleep(5)
    raise AssertionError(f"GET {url} did not succeed within {overall_timeout}s: {last_error}")


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def check_health(
    api_url: str,
    timeout: float,
    expect_commit: str | None,
    expect_environment: str | None,
) -> None:
    url = f"{api_url.rstrip('/')}/health"
    status, body = _retry_get(url, timeout)
    data = json.loads(body)

    commit = data.get("commit")
    print(f"OK  GET {url} -> {status} (commit {commit})")

    check(data.get("status") == "ok", f"expected status == 'ok', got {data.get('status')!r}")
    check(
        data.get("service") == "bookloud-api",
        f"expected service == 'bookloud-api', got {data.get('service')!r}",
    )
    if expect_environment is not None:
        check(
            data.get("environment") == expect_environment,
            f"expected environment == {expect_environment!r}, got {data.get('environment')!r}",
        )
    if expect_commit is not None:
        # The commit assertion is what proves the deploy actually shipped
        # *this* code, not a stale deploy.
        check(
            data.get("commit") == expect_commit,
            f"expected commit == {expect_commit!r}, got {data.get('commit')!r}",
        )


def check_frontend(frontend_url: str, timeout: float) -> None:
    url = frontend_url.rstrip("/") + "/"
    status, body = _retry_get(url, timeout)
    print(f"OK  GET {url} -> {status}")

    check("Bookloud" in body, "frontend response body does not contain 'Bookloud'")
    # Catches the "OpenNext bundle missing, CDK silently used the inline
    # placeholder" failure mode.
    check(
        "is deploying" not in body,
        "frontend response body contains the placeholder page ('is deploying') "
        "-- the real OpenNext build was not deployed",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--frontend-url", default=None)
    parser.add_argument("--expect-commit", default=None)
    parser.add_argument("--expect-environment", default=None)
    parser.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args()

    print(f"Smoke test against api={args.api_url} frontend={args.frontend_url or '(skipped)'}")

    check_health(args.api_url, args.timeout, args.expect_commit, args.expect_environment)

    if args.frontend_url:
        check_frontend(args.frontend_url, args.timeout)

    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nSMOKE TEST FAILED: {exc}")
        sys.exit(1)
