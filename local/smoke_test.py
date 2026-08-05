"""Smoke test proving the whole deploy loop works: push code -> CDK synth ->
deploy -> this script -> teardown.

Standard library only (urllib, json, argparse, time) so it runs from a clean
checkout, inside CI, with no install step.

Usage:
    python3 local/smoke_test.py \
        [--api-url URL]                 # default http://localhost:8000
        [--frontend-url URL]            # optional; skipped if omitted
        [--expect-commit SHA]           # optional
        [--expect-environment ENV]      # optional
        [--timeout SECONDS]             # default 180
        [--cognito-endpoint URL]        # optional; local/cognito-local. Derived from
                                         # --cognito-region against real AWS if omitted.
        [--cognito-region REGION]       # optional; used only to derive --cognito-endpoint
        [--cognito-client-id ID]        # optional; enables the wrong-password check
        [--auth-signup]                 # optional; signs up + logs in a throwaway user
        [--login-username USER]         # optional; logs in an existing user (e.g. local's seeded "dev")
        [--login-password PASS]
        [--newuser-username USER]       # optional; exercises the NEW_PASSWORD_REQUIRED
        [--newuser-temp-password PASS]  # challenge flow for an admin-provisioned user
                                         # (e.g. local's seeded "newuser", PLANS/phase-1.md §11)

Auth checks (PLANS/phase-1.md §6.1) are all skipped unless their inputs are
supplied, except the anonymous-401 check, which always runs.
"""

from __future__ import annotations

import argparse
import json
import random
import string
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


def _cognito(endpoint: str, target: str, body: dict, timeout: float = 15) -> tuple[int, dict]:
    """Plain Cognito JSON API call: POST {endpoint} with an X-Amz-Target
    header. No SigV4 -- the app client has no secret."""
    req = urllib.request.Request(
        endpoint,
        method="POST",
        headers={
            "Content-Type": "application/x-amz-json-1.1",
            "X-Amz-Target": f"AWSCognitoIdentityProviderService.{target}",
        },
        data=json.dumps(body).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8") or "{}")


def check_anonymous_me_rejected(api_url: str, timeout: float) -> None:
    """Always runs: GET /me with no Authorization header must be 401. This
    is the "protected route rejection without token" check at the deployed
    (or local compose) infra layer."""
    url = f"{api_url.rstrip('/')}/me"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    print(f"OK  GET {url} (no token) -> {status}")
    # Assert on the status code, not the body: API Gateway's 401 body
    # ({"message":"Unauthorized"}) differs from FastAPI's local-dev shape.
    check(status == 401, f"expected 401 for anonymous GET /me, got {status}")


def check_wrong_password_rejected(cognito_endpoint: str, cognito_client_id: str) -> None:
    """A deliberately wrong password must be rejected with
    NotAuthorizedException (Cognito's generic "bad credentials" error,
    because the pool has prevent_user_existence_errors=True)."""
    status, data = _cognito(
        cognito_endpoint,
        "InitiateAuth",
        {
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": cognito_client_id,
            "AuthParameters": {
                "USERNAME": f"smoke-nonexistent-{int(time.time())}",
                "PASSWORD": "definitely-the-wrong-password",
            },
        },
    )
    error_type = str(data.get("__type", ""))
    print(f"OK  InitiateAuth (wrong password) -> {status} {error_type}")
    check(not (200 <= status < 300), f"expected a non-2xx status, got {status}")
    check(
        "NotAuthorizedException" in error_type,
        f"expected NotAuthorizedException, got {error_type!r}",
    )


def _login_and_check_me(
    api_url: str, cognito_endpoint: str, cognito_client_id: str, username: str, password: str
) -> None:
    status, data = _cognito(
        cognito_endpoint,
        "InitiateAuth",
        {
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": cognito_client_id,
            "AuthParameters": {"USERNAME": username, "PASSWORD": password},
        },
    )
    check(200 <= status < 300, f"InitiateAuth for {username!r} failed: {status} {data}")
    id_token = data["AuthenticationResult"]["IdToken"]

    url = f"{api_url.rstrip('/')}/me"
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {id_token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.status
        body = json.loads(resp.read().decode("utf-8"))
    print(f"OK  GET {url} (as {username}) -> {status} (username={body.get('username')})")
    check(status == 200, f"expected 200 for authenticated GET /me, got {status}")
    check(
        body.get("username") == username,
        f"expected username == {username!r}, got {body.get('username')!r}",
    )


def check_new_password_challenge_flow(
    api_url: str,
    cognito_endpoint: str,
    cognito_client_id: str,
    username: str,
    temp_password: str,
) -> None:
    """Admin-provisioned users (PLANS/phase-1.md §11) are created with a
    temporary, non-permanent password: their first InitiateAuth returns
    NEW_PASSWORD_REQUIRED + a Session token instead of tokens. This proves
    the whole forced-first-login flow end to end against a real (or
    emulated) Cognito: challenge -> RespondToAuthChallenge -> /me 200."""
    status, data = _cognito(
        cognito_endpoint,
        "InitiateAuth",
        {
            "AuthFlow": "USER_PASSWORD_AUTH",
            "ClientId": cognito_client_id,
            "AuthParameters": {"USERNAME": username, "PASSWORD": temp_password},
        },
    )
    check(200 <= status < 300, f"InitiateAuth for {username!r} failed: {status} {data}")
    check(
        data.get("ChallengeName") == "NEW_PASSWORD_REQUIRED",
        f"expected ChallengeName == 'NEW_PASSWORD_REQUIRED', got {data.get('ChallengeName')!r}",
    )
    session = data["Session"]
    print(f"OK  InitiateAuth ({username}, temp password) -> {status} ChallengeName=NEW_PASSWORD_REQUIRED")

    new_password = "Sm0ke-" + "".join(random.choices(string.ascii_letters + string.digits, k=12))
    status, data = _cognito(
        cognito_endpoint,
        "RespondToAuthChallenge",
        {
            "ClientId": cognito_client_id,
            "ChallengeName": "NEW_PASSWORD_REQUIRED",
            "ChallengeResponses": {"USERNAME": username, "NEW_PASSWORD": new_password},
            "Session": session,
        },
    )
    check(
        200 <= status < 300,
        f"RespondToAuthChallenge for {username!r} failed: {status} {data}",
    )
    id_token = data["AuthenticationResult"]["IdToken"]
    print(f"OK  RespondToAuthChallenge ({username}) -> {status}")

    url = f"{api_url.rstrip('/')}/me"
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {id_token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.status
        body = json.loads(resp.read().decode("utf-8"))
    print(f"OK  GET {url} (as {username}, post-challenge) -> {status} (username={body.get('username')})")
    check(status == 200, f"expected 200 for authenticated GET /me, got {status}")
    check(
        body.get("username") == username,
        f"expected username == {username!r}, got {body.get('username')!r}",
    )


def check_signup_login_flow(api_url: str, cognito_endpoint: str, cognito_client_id: str) -> None:
    """SignUp a throwaway user, then log in and hit /me -- covers "signup" +
    "login" + authenticated access in one deployed-environment check. Never
    run against prod (only deploy-pr.yml passes --auth-signup)."""
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    username = f"smoke-{suffix}-{int(time.time())}"
    password = "Sm0ke-" + "".join(random.choices(string.ascii_letters + string.digits, k=12))

    status, data = _cognito(
        cognito_endpoint,
        "SignUp",
        {"ClientId": cognito_client_id, "Username": username, "Password": password},
    )
    check(200 <= status < 300, f"SignUp for {username!r} failed: {status} {data}")
    print(f"OK  SignUp {username} -> {status} (UserConfirmed={data.get('UserConfirmed')})")

    _login_and_check_me(api_url, cognito_endpoint, cognito_client_id, username, password)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument("--frontend-url", default=None)
    parser.add_argument("--expect-commit", default=None)
    parser.add_argument("--expect-environment", default=None)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--cognito-endpoint", default=None)
    parser.add_argument("--cognito-region", default=None)
    parser.add_argument("--cognito-client-id", default=None)
    parser.add_argument("--auth-signup", action="store_true")
    parser.add_argument("--login-username", default=None)
    parser.add_argument("--login-password", default=None)
    parser.add_argument("--newuser-username", default=None)
    parser.add_argument("--newuser-temp-password", default=None)
    args = parser.parse_args()

    print(f"Smoke test against api={args.api_url} frontend={args.frontend_url or '(skipped)'}")

    check_health(args.api_url, args.timeout, args.expect_commit, args.expect_environment)

    if args.frontend_url:
        check_frontend(args.frontend_url, args.timeout)

    # Always: prove the authorizer rejects anonymous requests.
    check_anonymous_me_rejected(args.api_url, args.timeout)

    if args.cognito_client_id:
        cognito_endpoint = args.cognito_endpoint or (
            f"https://cognito-idp.{args.cognito_region}.amazonaws.com" if args.cognito_region else None
        )
        if not cognito_endpoint:
            raise SystemExit("--cognito-client-id requires --cognito-endpoint or --cognito-region")

        check_wrong_password_rejected(cognito_endpoint, args.cognito_client_id)

        if args.auth_signup:
            check_signup_login_flow(args.api_url, cognito_endpoint, args.cognito_client_id)

        if args.login_username and args.login_password:
            _login_and_check_me(
                args.api_url,
                cognito_endpoint,
                args.cognito_client_id,
                args.login_username,
                args.login_password,
            )

        if args.newuser_username and args.newuser_temp_password:
            check_new_password_challenge_flow(
                args.api_url,
                cognito_endpoint,
                args.cognito_client_id,
                args.newuser_username,
                args.newuser_temp_password,
            )

    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError as exc:
        print(f"\nSMOKE TEST FAILED: {exc}")
        sys.exit(1)
