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
        [--extraction-timeout SECONDS]  # default 120; how long to poll GET /books/{id}
                                         # for EXTRACTED/FAILED in check_upload_and_synthesis
        [--synthesis-timeout SECONDS]   # default 180; how long to poll GET /books/{id}/chunks
                                         # for every chunk to reach a terminal state
        [--skip-synthesis]              # optional; skips the synthesis-phase assertions --
                                         # a cheap way to shorten local iteration, not needed
                                         # for flakiness reasons (see PLANS/phase-4.md §0)
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

Library checks (PLANS/phase-2.md §8): anonymous `GET /books` -> 401 always
runs; `GET /books` -> 200 + JSON array runs whenever --login-username/
--login-password are supplied (piggybacking on the same login the auth
checks use), proving the Library context's DynamoDB repository works
against a real DynamoDB API (LocalStack in local-smoke, real AWS in
deploy-pr/deploy-prod).

Upload/extraction/synthesis check (PLANS/phase-3.md §9.3, extended by
PLANS/phase-4.md §0/§9.3): `check_upload_and_synthesis` (renamed from
`check_upload_and_extraction`) runs whenever --login-username/
--login-password are supplied -- POST /books -> upload the embedded fixture
PDF via the presigned POST -> poll GET /books/{id} until EXTRACTED/FAILED ->
GET /books/{id}/chunks -> poll chunks to a terminal state (DONE or FAILED).
This is the phase's real gate: it proves the S3 event notification, the
extract Lambda/local-worker, PyMuPDF extraction, the header/footer filter,
chunking, the synthesis fan-out, the synthesize Lambda/local-worker, and the
chunksDone/chunksTotal/chunksFailed counters all work end to end against
real (or LocalStack) infra.

**Real TTS engines are prod-only (PLANS/phase-4.md §0).** In every
environment this smoke test runs against (`local-smoke`'s LocalStack
instance and `deploy-pr`'s real-but-ephemeral AWS stack --
`deploy-prod.yml` never runs this login-gated check at all, unchanged from
every prior phase), `get_speech_synthesizer()` returns a `StubSynthesizer`.
So the synthesis assertions are deterministic and engine-agnostic: every
chunk reaches `FAILED`/`EXTERNAL_TTS_DISABLED` (not `DONE` -- there is no
real audio to expect here), with `audioKey`/`marksKey`/`durationMs` still
null/zero, and `chunksDone == chunksTotal == chunksFailed` -- the load-
bearing fan-in proof that would have caught every ordering bug in the fan-
out/claim/counter machinery. No live-endpoint dependency anywhere in CI.
"""

from __future__ import annotations

import argparse
import base64
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


# A 3-page PDF: a running header ("Bookloud Smoke Test Guide") + a bare
# footer page number on every page, and a distinct "Bookloud smoke test"
# marker in each page's body text. Exercising the header/footer filter needs
# >=3 pages (domain/layout.py's RUNNING_LINE_MIN_PAGES) for the *repeated
# header* to be dropped -- the footer page number is dropped unconditionally
# regardless of page count, but the header alone wouldn't prove much on a
# 1-2 page doc, hence 3 rather than the "two-page" figure in PLANS/phase-3.md
# §9.3's description (flagged here as a deliberate, harmless deviation).
#
# Regenerated with (from backend/, inside the project's venv):
#   import pymupdf, io, base64
#   doc = pymupdf.open()
#   for i in range(3):
#       page = doc.new_page(width=595, height=842)
#       page.insert_text((72, 30), "Bookloud Smoke Test Guide", fontsize=10)
#       page.insert_text((72, 400),
#           f"Bookloud smoke test body content on page {i + 1} of 3. This "
#           "paragraph proves extraction, chunking, and the header and "
#           "footer filter all ran inside the deployed extract Lambda.",
#           fontsize=11)
#       page.insert_text((72, 820), f"Page {i + 1}", fontsize=9)
#   buf = io.BytesIO(); doc.save(buf)
#   print(base64.b64encode(buf.getvalue()).decode("ascii"))
_SMOKE_PDF_B64 = (
    "JVBERi0xLjcKJcK1wrYKJSBXcml0dGVuIGJ5IE11UERGIDEuMjkuMAoKMSAwIG9iago8PC9UeXBl"
    "L0NhdGFsb2cvUGFnZXMgMiAwIFIvSW5mbzw8L1Byb2R1Y2VyKE11UERGIDEuMjkuMCk+Pj4+CmVu"
    "ZG9iagoKMiAwIG9iago8PC9UeXBlL1BhZ2VzL0NvdW50IDMvS2lkc1s0IDAgUiAxMCAwIFIgMTUg"
    "MCBSXT4+CmVuZG9iagoKMyAwIG9iago8PC9Gb250PDwvaGVsdiA1IDAgUj4+Pj4KZW5kb2JqCgo0"
    "IDAgb2JqCjw8L1R5cGUvUGFnZS9NZWRpYUJveFswIDAgNTk1IDg0Ml0vUm90YXRlIDAvUmVzb3Vy"
    "Y2VzIDMgMCBSL1BhcmVudCAyIDAgUi9Db250ZW50c1s2IDAgUiA3IDAgUiA4IDAgUl0+PgplbmRv"
    "YmoKCjUgMCBvYmoKPDwvVHlwZS9Gb250L1N1YnR5cGUvVHlwZTEvQmFzZUZvbnQvSGVsdmV0aWNh"
    "L0VuY29kaW5nL1dpbkFuc2lFbmNvZGluZz4+CmVuZG9iagoKNiAwIG9iago8PC9MZW5ndGggOTUv"
    "RmlsdGVyL0ZsYXRlRGVjb2RlPj4Kc3RyZWFtCnja4yrkcgrhMlQwAEJDBXMjBQtDI4WQXC79jNSc"
    "MgVDA4WQNIVoGxMjszQgTDJLNkszNzUzMTIwNTZLAYuYAtkmZqbmxuZAURNzoKylGZBvFxvixeUa"
    "whXIBQBNrRZ0CmVuZHN0cmVhbQplbmRvYmoKCjcgMCBvYmoKPDwvTGVuZ3RoIDIxNS9GaWx0ZXIv"
    "RmxhdGVEZWNvZGU+PgpzdHJlYW0KeNpVULFKhjEM3PsUfQPbNL20IA6Ci5vQTVzs/3046ODi83uJ"
    "yK8UwuVySa5Jn+l+pZoLX80mWVXy+kg3b8f7V641rzM/36rg5HvFxmkdKsUaLsF0YkW3ZmThOrVJ"
    "1IgOr3hkzozKggrznlaDA1ETVrpiYFr70Zi4ziOz4RzxafA97Os2TEPDrZgxe8fOQXcHXU1GC64S"
    "aXgcvtcjOXcs1yrg/7NfFuzfdtVscFLso1Ofzd9Pn/FnrqtL3GciLvTfpRTdRBdeSFHluHtZj+lh"
    "paf0DSqBVxgKZW5kc3RyZWFtCmVuZG9iagoKOCAwIG9iago8PC9MZW5ndGggNTg+PgpzdHJlYW0K"
    "CnEKQlQKMSAwIDAgMSA3MiAyMiBUbQovaGVsdiA5IFRmIFs8NTA2MTY3NjUyMDMxPl1USgpFVApR"
    "CgplbmRzdHJlYW0KZW5kb2JqCgo5IDAgb2JqCjw8L0ZvbnQ8PC9oZWx2IDUgMCBSPj4+PgplbmRv"
    "YmoKCjEwIDAgb2JqCjw8L1R5cGUvUGFnZS9NZWRpYUJveFswIDAgNTk1IDg0Ml0vUm90YXRlIDAv"
    "UmVzb3VyY2VzIDkgMCBSL1BhcmVudCAyIDAgUi9Db250ZW50c1sxMSAwIFIgMTIgMCBSIDEzIDAg"
    "Ul0+PgplbmRvYmoKCjExIDAgb2JqCjw8L0xlbmd0aCA5NS9GaWx0ZXIvRmxhdGVEZWNvZGU+Pgpz"
    "dHJlYW0KeNrjKuRyCuEyVDAAQkMFcyMFC0MjhZBcLv2M1JwyBUMDhZA0hWgbEyOzNCBMMks2SzM3"
    "NTMxMjA1NksBi5gC2SZmpubG5kBRE3OgrKUZkG8XG+LF5RrCFcgFAE2tFnQKZW5kc3RyZWFtCmVu"
    "ZG9iagoKMTIgMCBvYmoKPDwvTGVuZ3RoIDIxNS9GaWx0ZXIvRmxhdGVEZWNvZGU+PgpzdHJlYW0K"
    "eNpVULFKhjEM3PsUfQPbNL20IA6Ci5vQTVzs/3046ODi83uJyK8UwuVySa5Jn+l+pZoLX80mWVXy"
    "+kg3b8f7V641rzM/36rg5HvFxmkdKsUaLsF0YkW3ZmThOrVJ1IgOr3hkzozKggrznibBgagJK10x"
    "MK39aExc55HZcI74NPge9nUbpqHhVsyYvWPnoLuDriajBVeJNDwO3+uRnDuWaxXw/9kvC/Zvu2o2"
    "OCn20anP5u+nz/gz19Ul7jMRF/rvUopuogsvpKhy3L2sx/Sw0lP6BiuKVxkKZW5kc3RyZWFtCmVu"
    "ZG9iagoKMTMgMCBvYmoKPDwvTGVuZ3RoIDU4Pj4Kc3RyZWFtCgpxCkJUCjEgMCAwIDEgNzIgMjIg"
    "VG0KL2hlbHYgOSBUZiBbPDUwNjE2NzY1MjAzMj5dVEoKRVQKUQoKZW5kc3RyZWFtCmVuZG9iagoK"
    "MTQgMCBvYmoKPDwvRm9udDw8L2hlbHYgNSAwIFI+Pj4+CmVuZG9iagoKMTUgMCBvYmoKPDwvVHlw"
    "ZS9QYWdlL01lZGlhQm94WzAgMCA1OTUgODQyXS9Sb3RhdGUgMC9SZXNvdXJjZXMgMTQgMCBSL1Bh"
    "cmVudCAyIDAgUi9Db250ZW50c1sxNiAwIFIgMTcgMCBSIDE4IDAgUl0+PgplbmRvYmoKCjE2IDAg"
    "b2JqCjw8L0xlbmd0aCA5NS9GaWx0ZXIvRmxhdGVEZWNvZGU+PgpzdHJlYW0KeNrjKuRyCuEyVDAA"
    "QkMFcyMFC0MjhZBcLv2M1JwyBUMDhZA0hWgbEyOzNCBMMks2SzM3NTMxMjA1NksBi5gC2SZmpubG"
    "5kBRE3OgrKUZkG8XG+LF5RrCFcgFAE2tFnQKZW5kc3RyZWFtCmVuZG9iagoKMTcgMCBvYmoKPDwv"
    "TGVuZ3RoIDIxNi9GaWx0ZXIvRmxhdGVEZWNvZGU+PgpzdHJlYW0KeNpVUL1qgzEM3P0UfoP4Rz7Z"
    "UDIUumQLeCtd6nwfHdohS5+/J5WQBIM4nU7SWeEaXmfIMfHlqCWKlDh/wuFr+/6NOce5x/cXKdj5"
    "PrGwa4OUpBUXZxqxoGlVsjCd6CCqRJtVLDJnRmVChlpPrc7BEStN0DG0/mu0mM4is24c8a6wPexr"
    "2lVcw60YPnv5zk53G10NRnUuE4l77LbXIjlzXO5VwP6nNxbsX3rXLHCS76NTm83fD5vxMNfUye8z"
    "4Bd6dlmSLKILLyTIZTt+zFN4m+Ec/gAsk1caCmVuZHN0cmVhbQplbmRvYmoKCjE4IDAgb2JqCjw8"
    "L0xlbmd0aCA1OD4+CnN0cmVhbQoKcQpCVAoxIDAgMCAxIDcyIDIyIFRtCi9oZWx2IDkgVGYgWzw1"
    "MDYxNjc2NTIwMzM+XVRKCkVUClEKCmVuZHN0cmVhbQplbmRvYmoKCnhyZWYKMCAxOQowMDAwMDAw"
    "MDAwIDY1NTM1IGYgCjAwMDAwMDAwNDIgMDAwMDAgbiAKMDAwMDAwMDEyMCAwMDAwMCBuIAowMDAw"
    "MDAwMTg2IDAwMDAwIG4gCjAwMDAwMDAyMjcgMDAwMDAgbiAKMDAwMDAwMDM0NiAwMDAwMCBuIAow"
    "MDAwMDAwNDM1IDAwMDAwIG4gCjAwMDAwMDA1OTggMDAwMDAgbiAKMDAwMDAwMDg4MiAwMDAwMCBu"
    "IAowMDAwMDAwOTg5IDAwMDAwIG4gCjAwMDAwMDEwMzAgMDAwMDAgbiAKMDAwMDAwMTE1MyAwMDAw"
    "MCBuIAowMDAwMDAxMzE3IDAwMDAwIG4gCjAwMDAwMDE2MDIgMDAwMDAgbiAKMDAwMDAwMTcxMCAw"
    "MDAwMCBuIAowMDAwMDAxNzUyIDAwMDAwIG4gCjAwMDAwMDE4NzYgMDAwMDAgbiAKMDAwMDAwMjA0"
    "MCAwMDAwMCBuIAowMDAwMDAyMzI2IDAwMDAwIG4gCgp0cmFpbGVyCjw8L1NpemUgMTkvUm9vdCAx"
    "IDAgUi9JRFs8QzM4MzI4MUMwNEMzQkU1MjEzQzM5QTQwQzI4QjA5QzI+PDM5QzgzQTY4REZGOTMz"
    "ODkyMDM1N0MxODIwRDk0NDFBPl0+PgpzdGFydHhyZWYKMjQzNAolJUVPRgo="
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


def check_books_endpoint(api_url: str, timeout: float) -> None:
    """PLANS/phase-2.md §8/§9: anonymous GET /books -> 401 (same authorizer
    boundary as GET /me); authenticated GET /books -> 200 with a JSON array.
    This is what proves the Library context's DynamoDB repository actually
    works against a real DynamoDB API (LocalStack or real AWS) rather than
    just moto -- the TABLE_NAME env var, the IAM grant, and the deployed
    table's key schema all get exercised here."""
    url = f"{api_url.rstrip('/')}/books"

    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    print(f"OK  GET {url} (no token) -> {status}")
    check(status == 401, f"expected 401 for anonymous GET /books, got {status}")


def _login(cognito_endpoint: str, cognito_client_id: str, username: str, password: str) -> str:
    """``InitiateAuth`` with ``USER_PASSWORD_AUTH`` for an existing,
    permanent-password user; returns the id token. Shared by every check
    below that needs an authenticated request -- previously duplicated
    inline in each one."""
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
    return data["AuthenticationResult"]["IdToken"]


def check_books_endpoint_authenticated(
    api_url: str, cognito_endpoint: str, cognito_client_id: str, username: str, password: str
) -> None:
    id_token = _login(cognito_endpoint, cognito_client_id, username, password)

    url = f"{api_url.rstrip('/')}/books"
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {id_token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        status = resp.status
        body = json.loads(resp.read().decode("utf-8"))
    print(f"OK  GET {url} (as {username}) -> {status} ({len(body)} book(s))")
    check(status == 200, f"expected 200 for authenticated GET /books, got {status}")
    check(isinstance(body, list), f"expected a JSON array, got {type(body).__name__}")


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
    id_token = _login(cognito_endpoint, cognito_client_id, username, password)

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


def _build_multipart_upload(fields: dict, pdf_bytes: bytes) -> tuple[bytes, str]:
    """Hand-build a ``multipart/form-data`` body for the presigned POST
    (stdlib only, no ``requests``/``urllib3`` multipart helper available):
    every entry of ``fields`` first, the ``file`` field **last** -- S3
    ignores anything after the file field, so ordering here is load-bearing,
    matching the client contract PLANS/phase-3.md §4.1 documents for the
    phase 6 frontend."""
    boundary = "----bookloudSmokeTestBoundary"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="source.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode("utf-8")
    )
    parts.append(pdf_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _authed_get(url: str, id_token: str, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {id_token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _poll_book_status(api_url: str, book_id: str, id_token: str, timeout: float) -> dict:
    url = f"{api_url.rstrip('/')}/books/{book_id}"
    deadline = time.monotonic() + timeout
    last_body: dict | None = None
    while time.monotonic() < deadline:
        status, body = _authed_get(url, id_token, 10)
        check(status == 200, f"GET {url} failed while polling: {status} {body}")
        last_body = json.loads(body)
        if last_body["status"] in ("EXTRACTED", "FAILED"):
            return last_body
        time.sleep(3)
    raise AssertionError(
        f"extraction did not reach EXTRACTED/FAILED within {timeout}s; last status: {last_body}"
    )


def _poll_chunks_to_terminal(api_url: str, book_id: str, id_token: str, timeout: float) -> list[dict]:
    """Poll GET /books/{id}/chunks every 3s until every chunk's status is
    DONE or FAILED, or the timeout expires (PLANS/phase-4.md §9.3)."""
    url = f"{api_url.rstrip('/')}/books/{book_id}/chunks"
    deadline = time.monotonic() + timeout
    last_chunks: list[dict] = []
    while time.monotonic() < deadline:
        status, body = _authed_get(url, id_token, 15)
        check(status == 200, f"GET {url} failed while polling: {status} {body}")
        last_chunks = json.loads(body)
        if last_chunks and all(c["status"] in ("DONE", "FAILED") for c in last_chunks):
            return last_chunks
        time.sleep(3)
    raise AssertionError(
        f"synthesis did not reach a terminal state for every chunk within {timeout}s; "
        f"last statuses: {[c.get('status') for c in last_chunks]}"
    )


def check_upload_and_synthesis(
    api_url: str,
    cognito_endpoint: str,
    cognito_client_id: str,
    username: str,
    password: str,
    extraction_timeout: float,
    synthesis_timeout: float,
    *,
    skip_synthesis: bool = False,
) -> None:
    """PLANS/phase-3.md §9.3, extended by PLANS/phase-4.md §0/§9.3 -- the
    phase's real gate. POST /books -> upload the embedded fixture PDF via
    the presigned POST -> poll GET /books/{id} until EXTRACTED/FAILED ->
    GET /books/{id}/chunks, asserting the extracted text carries the
    fixture's marker string. Proves the S3 event notification, the extract
    Lambda (or local worker), PyMuPDF extraction, the header/footer filter,
    and chunking all work end to end.

    Then (unless skip_synthesis): poll chunks to a terminal state and assert
    against a stub engine (real edge-tts/Google are prod-only, §0) -- every
    chunk FAILED/EXTERNAL_TTS_DISABLED, audioKey/marksKey/durationMs still
    null/zero, and chunksDone == chunksTotal == chunksFailed on the book."""
    id_token = _login(cognito_endpoint, cognito_client_id, username, password)

    create_url = f"{api_url.rstrip('/')}/books"
    create_req = urllib.request.Request(
        create_url,
        method="POST",
        headers={"Authorization": f"Bearer {id_token}", "Content-Type": "application/json"},
        data=json.dumps({"title": "Smoke Test Upload"}).encode("utf-8"),
    )
    with urllib.request.urlopen(create_req, timeout=15) as resp:
        create_status = resp.status
        create_body = json.loads(resp.read().decode("utf-8"))
    check(create_status == 201, f"expected 201 creating book, got {create_status}")
    book_id = create_body["book"]["id"]
    upload = create_body["upload"]
    print(f"OK  POST {create_url} -> {create_status} (book {book_id})")

    pdf_bytes = base64.b64decode(_SMOKE_PDF_B64)
    multipart_body, content_type = _build_multipart_upload(upload["fields"], pdf_bytes)
    upload_req = urllib.request.Request(
        upload["url"], method="POST", data=multipart_body, headers={"Content-Type": content_type}
    )
    try:
        with urllib.request.urlopen(upload_req, timeout=30) as resp:
            upload_status = resp.status
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AssertionError(f"presigned upload POST failed: {exc.code} {detail}") from exc
    print(f"OK  POST {upload['url']} (presigned upload) -> {upload_status}")
    check(upload_status == 204, f"expected 204 from the presigned upload, got {upload_status}")

    final = _poll_book_status(api_url, book_id, id_token, extraction_timeout)
    print(
        f"OK  extraction finished -> status={final['status']} "
        f"chunksTotal={final.get('chunksTotal')} pageCount={final.get('pageCount')}"
    )
    check(
        final["status"] == "EXTRACTED",
        f"expected EXTRACTED, got {final['status']} (failureReason={final.get('failureReason')})",
    )
    check(final.get("chunksTotal", 0) >= 1, "expected chunksTotal >= 1")
    check(final.get("pageCount", 0) >= 1, "expected pageCount >= 1")

    chunks_url = f"{api_url.rstrip('/')}/books/{book_id}/chunks"
    status, body = _authed_get(chunks_url, id_token, 15)
    check(status == 200, f"expected 200 for GET {chunks_url}, got {status}")
    chunks = json.loads(body)
    check(len(chunks) >= 1, "expected at least one chunk")
    full_text = "".join(chunk.get("text", "") for chunk in chunks)
    check(
        "Bookloud smoke test" in full_text,
        "extracted chunk text does not contain the fixture's marker string",
    )
    print(f"OK  GET {chunks_url} -> {status} ({len(chunks)} chunk(s))")

    if skip_synthesis:
        print("SKIP synthesis phase (--skip-synthesis)")
        return

    # --- synthesis phase (PLANS/phase-4.md §0/§9.3) -------------------------
    # Real edge-tts/Google are prod-only -- every environment this script
    # runs against gets a StubSynthesizer, so the outcome is fully
    # deterministic: every chunk reaches FAILED/EXTERNAL_TTS_DISABLED, never
    # DONE, since there is no real audio to expect here.
    terminal_chunks = _poll_chunks_to_terminal(api_url, book_id, id_token, synthesis_timeout)
    sources = {c.get("index"): c.get("synthesisSource") for c in terminal_chunks}
    print(f"OK  synthesis reached a terminal state for {len(terminal_chunks)} chunk(s); sources={sources}")

    for chunk in terminal_chunks:
        check(
            chunk["status"] == "FAILED",
            f"chunk {chunk.get('index')}: expected FAILED (stub-only environment), got {chunk['status']}",
        )
        check(
            chunk.get("failureReason") == "EXTERNAL_TTS_DISABLED",
            f"chunk {chunk.get('index')}: expected failureReason == 'EXTERNAL_TTS_DISABLED', "
            f"got {chunk.get('failureReason')!r}",
        )
        check(chunk.get("audioKey") is None, f"chunk {chunk.get('index')}: expected audioKey to still be null")
        check(chunk.get("marksKey") is None, f"chunk {chunk.get('index')}: expected marksKey to still be null")
        check(
            not chunk.get("durationMs"),
            f"chunk {chunk.get('index')}: expected durationMs to still be 0/null",
        )

    # The real fan-in proof: this is the assertion that would have caught
    # every ordering bug in the fan-out/claim/counter machinery.
    final_book = _poll_book_status(api_url, book_id, id_token, 15)
    chunks_done = final_book.get("chunksDone", 0)
    chunks_total = final_book.get("chunksTotal", 0)
    chunks_failed = final_book.get("chunksFailed", 0)
    print(f"OK  book counters -> chunksDone={chunks_done} chunksTotal={chunks_total} chunksFailed={chunks_failed}")
    check(
        chunks_done == chunks_total == chunks_failed and chunks_total >= 1,
        f"expected chunksDone == chunksTotal == chunksFailed >= 1, got "
        f"chunksDone={chunks_done} chunksTotal={chunks_total} chunksFailed={chunks_failed}",
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
    parser.add_argument("--extraction-timeout", type=float, default=120)
    parser.add_argument("--synthesis-timeout", type=float, default=180)
    parser.add_argument("--skip-synthesis", action="store_true")
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
    check_books_endpoint(args.api_url, args.timeout)

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
            check_books_endpoint_authenticated(
                args.api_url,
                cognito_endpoint,
                args.cognito_client_id,
                args.login_username,
                args.login_password,
            )
            check_upload_and_synthesis(
                args.api_url,
                cognito_endpoint,
                args.cognito_client_id,
                args.login_username,
                args.login_password,
                args.extraction_timeout,
                args.synthesis_timeout,
                skip_synthesis=args.skip_synthesis,
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
