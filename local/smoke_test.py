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
                                         # for EXTRACTED/FAILED in check_upload_and_stitch
        [--synthesis-timeout SECONDS]   # default 180; how long to poll GET /books/{id}/chunks
                                         # for every chunk to reach a terminal state
        [--stitch-timeout SECONDS]      # default 120; how long to poll
                                         # GET /books/{id}/status for terminal is true
        [--expect-synthesis MODE]       # failed (default) | silent. See "Two synthesis
                                         # modes" below.
        [--skip-synthesis]              # optional; skips the synthesis/stitch assertions --
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
        [--extract-function-name NAME]     # optional; deployed Lambda names to invoke
        [--synthesize-function-name NAME]  # directly on every poll iteration instead of
        [--stitch-function-name NAME]      # waiting on that Lambda's own EventBridge Rule
                                            # schedule. Passed by deploy-pr/deploy-prod only
                                            # -- see check_upload_and_stitch's docstring.

Auth checks (PLANS/phase-1.md §6.1) are all skipped unless their inputs are
supplied, except the anonymous-401 check, which always runs.

Library checks (PLANS/phase-2.md §8): anonymous `GET /books` -> 401 always
runs; `GET /books` -> 200 + JSON array runs whenever --login-username/
--login-password are supplied (piggybacking on the same login the auth
checks use), proving the Library context's DynamoDB repository works
against a real DynamoDB API (LocalStack in local-smoke, real AWS in
deploy-pr/deploy-prod).

Upload/extraction/synthesis/stitch check (PLANS/phase-3.md §9.3, extended by
PLANS/phase-4.md §0/§9.3 and PLANS/phase-5.md §9.3):
`check_upload_and_stitch` (renamed from `check_upload_and_synthesis`) runs
whenever --login-username/--login-password are supplied -- POST /books ->
upload the embedded fixture PDF via the presigned POST -> poll
GET /books/{id} until EXTRACTED/FAILED -> GET /books/{id}/chunks -> poll
chunks to a terminal state (DONE or FAILED) -> poll
GET /books/{id}/status until `terminal` is true. This is the phase's real
gate: it proves the S3 event notification, the extract Lambda/local-worker,
PyMuPDF extraction, the header/footer filter, chunking, the synthesis
fan-out, the synthesize Lambda/local-worker, the chunksDone/chunksTotal/
chunksFailed counters, the fan-IN edge, the stitch queue, the stitch
Lambda/local-worker and its book.json artefact all work end to end against
real (or LocalStack) infra.

**Real TTS engines are prod-only (PLANS/phase-4.md §0).** In every
environment this smoke test runs against (`local-smoke`'s LocalStack
instance and `deploy-pr`'s real-but-ephemeral AWS stack --
`deploy-prod.yml` never runs this login-gated check at all, unchanged from
every prior phase), `get_speech_synthesizer()` never returns a real engine.

**Two synthesis modes, selected by --expect-synthesis:**

- `failed` (the default, and what `deploy-pr` uses): the deployed stack runs
  the raise-only `StubSynthesizer`, so every chunk reaches
  `FAILED`/`EXTERNAL_TTS_DISABLED` with `audioKey`/`marksKey`/`durationMs`
  still null/zero, `chunksDone == chunksTotal == chunksFailed`, and the book
  stitches to `PARTIAL`/`NO_AUDIO` with a real `book.json` and no
  `book.mp3`. That last assertion is the load-bearing one: `book.json` can
  only exist if the stitch Lambda ran, held `marks_bucket` PutObject,
  resolved MARKS_BUCKET, and completed its terminal DynamoDB transition.
- `silent` (what `make smoke` uses): docker-compose sets
  `SYNTHESIS_STUB_MODE=silent` on the local synthesize-worker, so chunks
  synthesize offline into real silent MPEG-2 frames and the stitcher really
  concatenates them -- the book reaches `READY` with a `book.mp3` and a
  non-zero `durationMs`. This is the only automated check anywhere that
  exercises the byte path (PLANS/phase-5.md OQ-1).

Either way there is no live-endpoint dependency anywhere in CI.

Audio-delivery and resynthesis checks (PLANS/phase-6.md §12.2), both
branching on the same --expect-synthesis flag:

- `check_audio_delivery` -- in `failed` mode, the three assertions a book
  with no audio can honestly make: `GET /books/{id}/audio` -> 409 (proving
  the endpoint exists, is authorized, and correctly refuses), the manifest is
  fetchable through the API and really is the zero-segment document phase-5
  §3.2 promises, and a chunk's marks are a 404. In `silent` mode it goes
  further and does the one thing **no unit test, mock or fake can do**: it
  fetches the presigned URL with a plain urllib GET carrying **no
  Authorization header**, then re-fetches it with `Range: bytes=0-1023` and
  asserts a `206` with exactly 1024 bytes. That is the whole of §3's argument
  -- a URL signed one way accepting an unsigned Range header -- and only a
  real S3 API can produce it.
- `check_resynthesize` -- in `failed` mode the book is PARTIAL/NO_AUDIO, so
  POST /books/{id}/resynthesize returns 202, the book rewinds to EXTRACTED,
  and polling it back to terminal returns PARTIAL/NO_AUDIO again. **The loop
  closes**, which proves the fan-out was really re-published and really
  consumed -- the only end-to-end proof of /resynthesize any automated check
  can produce, and a complete one. In `silent` mode the book is READY, so the
  same POST returns 409, proving the status guard from the other side.
"""

from __future__ import annotations

import argparse
import base64
import json
import random
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Sequence


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


def _invoke_lambda(function_name: str) -> None:
    """Synchronously invokes a deployed pipeline Lambda via the AWS CLI --
    this smoke test's substitute for waiting on that Lambda's own
    EventBridge Rule schedule (up to 20 minutes on the stitch queue,
    infra/stacks/config.py's ``*_POLL_INTERVAL_MINUTES``). The poll loops
    below call this every few seconds instead, so the deployed
    ``scheduled_handler`` code path still gets exercised for real -- CI just
    doesn't wait on the clock to trigger it. Raises on any AWS CLI failure:
    an invoke failing here means the deployed Lambda itself is broken, not
    something to shrug off."""
    result = subprocess.run(
        [
            "aws",
            "lambda",
            "invoke",
            "--function-name",
            function_name,
            "--invocation-type",
            "RequestResponse",
            "--cli-binary-format",
            "raw-in-base64-out",
            "--payload",
            "{}",
            "/dev/null",
        ],
        capture_output=True,
        text=True,
    )
    check(result.returncode == 0, f"aws lambda invoke {function_name!r} failed: {result.stderr}")


def _invoke_all(function_names: Sequence[str]) -> None:
    for name in function_names:
        if name:
            _invoke_lambda(name)


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


# Every status that means "extraction is finished, one way or another".
# Deliberately includes the phase-5 statuses: with the silent synthesizer a
# small book can race all the way to READY between two 3-second polls, so
# waiting for the literal string "EXTRACTED" would time out on a book that
# actually succeeded (PLANS/phase-5.md OQ-1).
_EXTRACTION_DONE_STATUSES = ("EXTRACTED", "STITCHING", "READY", "PARTIAL", "FAILED")


def _poll_book_status(
    api_url: str, book_id: str, id_token: str, timeout: float, *, invoke_functions: Sequence[str] = ()
) -> dict:
    url = f"{api_url.rstrip('/')}/books/{book_id}"
    deadline = time.monotonic() + timeout
    last_body: dict | None = None
    while time.monotonic() < deadline:
        _invoke_all(invoke_functions)
        status, body = _authed_get(url, id_token, 10)
        check(status == 200, f"GET {url} failed while polling: {status} {body}")
        last_body = json.loads(body)
        if last_body["status"] in _EXTRACTION_DONE_STATUSES:
            return last_body
        time.sleep(3)
    raise AssertionError(
        f"extraction did not finish within {timeout}s; last status: {last_body}"
    )


def _poll_chunks_to_terminal(
    api_url: str, book_id: str, id_token: str, timeout: float, *, invoke_functions: Sequence[str] = ()
) -> list[dict]:
    """Poll GET /books/{id}/chunks every 3s until every chunk's status is
    DONE or FAILED, or the timeout expires (PLANS/phase-4.md §9.3)."""
    url = f"{api_url.rstrip('/')}/books/{book_id}/chunks"
    deadline = time.monotonic() + timeout
    last_chunks: list[dict] = []
    while time.monotonic() < deadline:
        _invoke_all(invoke_functions)
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


def _poll_book_terminal(
    api_url: str, book_id: str, id_token: str, timeout: float, *, invoke_functions: Sequence[str] = ()
) -> dict:
    """Poll GET /books/{id}/status every 3s until the server says the book is
    terminal (PLANS/phase-5.md §8/§9.3). Polling the new endpoint IS part of
    the test -- it is the phase's other deliverable, and its server-computed
    `terminal` flag is what stops this loop from hanging forever on a PARTIAL
    book (the exact "frozen at 99%" failure mode phase-4 §8.3 eliminated)."""
    url = f"{api_url.rstrip('/')}/books/{book_id}/status"
    deadline = time.monotonic() + timeout
    last_body: dict | None = None
    while time.monotonic() < deadline:
        _invoke_all(invoke_functions)
        status, body = _authed_get(url, id_token, 15)
        check(status == 200, f"GET {url} failed while polling: {status} {body}")
        last_body = json.loads(body)
        if last_body.get("terminal") is True:
            return last_body
        time.sleep(3)
    raise AssertionError(
        f"book did not reach a terminal status within {timeout}s; last status: {last_body}"
    )


def check_upload_and_stitch(
    api_url: str,
    cognito_endpoint: str,
    cognito_client_id: str,
    username: str,
    password: str,
    extraction_timeout: float,
    synthesis_timeout: float,
    stitch_timeout: float,
    *,
    skip_synthesis: bool = False,
    expect_synthesis: str = "failed",
    extract_function: str = "",
    synthesize_function: str = "",
    stitch_function: str = "",
) -> str:
    """PLANS/phase-3.md §9.3, extended by PLANS/phase-4.md §0/§9.3 and
    PLANS/phase-5.md §9.3 -- the phase's real gate. POST /books -> upload the
    embedded fixture PDF via the presigned POST -> poll GET /books/{id} until
    EXTRACTED/FAILED -> GET /books/{id}/chunks, asserting the extracted text
    carries the fixture's marker string. Proves the S3 event notification, the
    extract Lambda (or local worker), PyMuPDF extraction, the header/footer
    filter, and chunking all work end to end.

    Then (unless skip_synthesis): poll chunks to a terminal state, then poll
    GET /books/{id}/status until `terminal`, asserting the outcome for
    `expect_synthesis` (see this module's docstring).

    ``extract_function``/``synthesize_function``/``stitch_function`` (empty
    by default): deployed Lambda function names to invoke directly on every
    poll iteration of their respective stage (via ``_invoke_all``). Real AWS
    runs (deploy-pr/deploy-prod) pass these -- the pipeline Lambdas only run
    on their own EventBridge Rule schedule now (up to 20 minutes on stitch),
    far longer than this script's polling timeouts should ever be sized for.
    local-smoke (LocalStack + the local docker-compose workers, which poll
    every second regardless) omits them; the loops behave exactly as before.
    """
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

    final = _poll_book_status(
        api_url, book_id, id_token, extraction_timeout, invoke_functions=(extract_function,)
    )
    print(
        f"OK  extraction finished -> status={final['status']} "
        f"chunksTotal={final.get('chunksTotal')} pageCount={final.get('pageCount')}"
    )
    check(
        final["status"] != "FAILED",
        f"extraction failed: {final['status']} (failureReason={final.get('failureReason')})",
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
    # Real edge-tts/Google are prod-only -- every environment this script runs
    # against gets an offline stand-in, so the outcome is fully deterministic
    # in both modes.
    terminal_chunks = _poll_chunks_to_terminal(
        api_url, book_id, id_token, synthesis_timeout, invoke_functions=(synthesize_function,)
    )
    sources = {c.get("index"): c.get("synthesisSource") for c in terminal_chunks}
    print(f"OK  synthesis reached a terminal state for {len(terminal_chunks)} chunk(s); sources={sources}")

    if expect_synthesis == "silent":
        _check_chunks_synthesized_silently(terminal_chunks)
    else:
        _check_chunks_failed_with_tts_disabled(terminal_chunks)

    # The real fan-in proof: this is the assertion that would have caught
    # every ordering bug in the fan-out/claim/counter machinery.
    counted_book = _poll_book_status(api_url, book_id, id_token, 15)
    chunks_done = counted_book.get("chunksDone", 0)
    chunks_total = counted_book.get("chunksTotal", 0)
    chunks_failed = counted_book.get("chunksFailed", 0)
    print(f"OK  book counters -> chunksDone={chunks_done} chunksTotal={chunks_total} chunksFailed={chunks_failed}")
    check(chunks_total >= 1, f"expected chunksTotal >= 1, got {chunks_total}")
    check(
        chunks_done == chunks_total,
        f"expected chunksDone == chunksTotal, got {chunks_done} != {chunks_total}",
    )
    expected_failed = 0 if expect_synthesis == "silent" else chunks_total
    check(
        chunks_failed == expected_failed,
        f"expected chunksFailed == {expected_failed} in {expect_synthesis!r} mode, got {chunks_failed}",
    )

    # --- stitch phase (PLANS/phase-5.md §9.3) --------------------------------
    status_body = _poll_book_terminal(
        api_url, book_id, id_token, stitch_timeout, invoke_functions=(stitch_function,)
    )
    print(
        f"OK  stitch finished -> status={status_body['status']} "
        f"terminal={status_body['terminal']} failureReason={status_body.get('failureReason')!r} "
        f"audio={status_body.get('audio')}"
    )

    progress = status_body.get("progress", {})
    check(
        progress
        == {
            "chunksTotal": chunks_total,
            "chunksDone": chunks_total,
            "chunksFailed": expected_failed,
            "percent": 100,
        },
        f"unexpected progress block: {progress}",
    )

    audio = status_body.get("audio", {})
    manifest_key = audio.get("manifestKey")
    # THE load-bearing artefact assertion: book.json can only exist if the
    # stitch Lambda ran, held marks_bucket PutObject, resolved MARKS_BUCKET,
    # and completed its terminal DynamoDB transition. Without it, "the
    # stitcher ran" would be inferred only from a status string the API could
    # in principle have produced some other way.
    check(
        isinstance(manifest_key, str) and manifest_key.endswith("/book.json"),
        f"expected audio.manifestKey to end in '/book.json', got {manifest_key!r}",
    )

    if expect_synthesis == "silent":
        check(
            status_body["status"] == "READY",
            f"expected READY with silent synthesis, got {status_body['status']} "
            f"(failureReason={status_body.get('failureReason')!r})",
        )
        check(
            status_body.get("failureReason") is None,
            f"expected failureReason to be null on a READY book, got {status_body.get('failureReason')!r}",
        )
        check(
            isinstance(audio.get("audioKey"), str) and audio["audioKey"].endswith("/book.mp3"),
            f"expected audio.audioKey to end in '/book.mp3', got {audio.get('audioKey')!r}",
        )
        check(
            audio.get("durationMs", 0) > 0,
            f"expected a non-zero stitched durationMs, got {audio.get('durationMs')!r}",
        )
    else:
        # The single assertion that proves §3.2's degraded path end to end:
        # the fan-in edge fired, a stitch message was published and consumed,
        # the claim and the terminal transition both ran -- and the book did
        # NOT become FAILED (which is re-claimable by the extract Lambda and
        # would wipe perfectly good text).
        check(
            status_body["status"] == "PARTIAL",
            f"expected PARTIAL with a stub-only engine, got {status_body['status']}",
        )
        check(
            status_body.get("failureReason") == "NO_AUDIO",
            f"expected failureReason == 'NO_AUDIO', got {status_body.get('failureReason')!r}",
        )
        check(
            audio.get("audioKey") is None,
            f"expected audio.audioKey to be null with no audio, got {audio.get('audioKey')!r}",
        )
        check(
            not audio.get("durationMs"),
            f"expected audio.durationMs == 0 with no audio, got {audio.get('durationMs')!r}",
        )

    # Cheap, and catches the two response shapes drifting apart.
    final_book = _poll_book_status(api_url, book_id, id_token, 15)
    check(
        final_book.get("status") == status_body["status"],
        f"GET /books/{{id}} status {final_book.get('status')!r} disagrees with "
        f"the status endpoint's {status_body['status']!r}",
    )
    check(
        final_book.get("audioKey") == audio.get("audioKey")
        and final_book.get("manifestKey") == manifest_key
        and final_book.get("audioDurationMs") == audio.get("durationMs"),
        f"GET /books/{{id}} audio fields disagree with the status endpoint: "
        f"{final_book.get('audioKey')!r}/{final_book.get('manifestKey')!r}/"
        f"{final_book.get('audioDurationMs')!r} vs {audio}",
    )
    print("OK  GET /books/{id} agrees with GET /books/{id}/status")

    # --- phase 6 (PLANS/phase-6.md §12.2) -----------------------------------
    # Run last, on the book this function just drove to a terminal state:
    # check_resynthesize MUTATES it (rewinding a PARTIAL book to EXTRACTED),
    # so nothing above may depend on its state afterwards.
    check_audio_delivery(api_url, book_id, id_token, expect_synthesis)
    check_resynthesize(
        api_url,
        book_id,
        id_token,
        expect_synthesis,
        stitch_timeout,
        synthesize_function=synthesize_function,
        stitch_function=stitch_function,
    )
    
    return book_id


# --- phase 6: audio delivery + resynthesis (PLANS/phase-6.md §12.2) ---------


def _unauthed_get(url: str, timeout: float, headers: dict | None = None):
    """A plain browser-shaped GET: **no Authorization header**. Returns
    ``(status, headers, body_bytes)``. Deliberately separate from
    ``_authed_get`` -- the entire point of the presigned URL is that it works
    without one, so a helper that quietly attached a token would make the
    assertion meaningless."""
    req = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def check_audio_delivery(api_url: str, book_id: str, id_token: str, expect_synthesis: str) -> None:
    base = f"{api_url.rstrip('/')}/books/{book_id}"

    if expect_synthesis != "silent":
        # The single assertion that proves the endpoint exists, is
        # authorized, and correctly refuses a book with no audio. This is the
        # ONLY audio-delivery path deploy-pr can reach, because every chunk
        # in a PR environment fails with EXTERNAL_TTS_DISABLED (phase-4 §0).
        status, body = _authed_get(f"{base}/audio", id_token, 15)
        check(status == 409, f"expected 409 from GET /books/{{id}}/audio with no audio, got {status} {body}")
        print(f"OK  GET {base}/audio -> 409 (no audio, as expected)")

        status, body = _authed_get(f"{base}/manifest", id_token, 15)
        check(status == 200, f"expected 200 from GET /books/{{id}}/manifest, got {status} {body}")
        manifest = json.loads(body)
        # The zero-segment document phase-5 §3.2 promises is real, reachable
        # through the API, and shaped the way phase 6's reader expects.
        check(manifest.get("segments") == [], f"expected segments == [] with no audio, got {manifest.get('segments')!r}")
        check(manifest.get("audioKey") is None, f"expected audioKey null with no audio, got {manifest.get('audioKey')!r}")
        check(not manifest.get("durationMs"), f"expected durationMs 0 with no audio, got {manifest.get('durationMs')!r}")
        expected_missing = list(range(manifest.get("chunksTotal", 0)))
        check(
            manifest.get("missing") == expected_missing,
            f"expected missing == {expected_missing}, got {manifest.get('missing')!r}",
        )
        print(f"OK  GET {base}/manifest -> 200 (zero-segment manifest, missing={manifest.get('missing')})")

        status, body = _authed_get(f"{base}/chunks/0/marks", id_token, 15)
        check(status == 404, f"expected 404 from GET /books/{{id}}/chunks/0/marks (chunk failed), got {status} {body}")
        print(f"OK  GET {base}/chunks/0/marks -> 404 (chunk failed, no marks object)")
        return

    # --- silent mode: local compose, the one place bytes exist -------------
    status, body = _authed_get(f"{base}/audio", id_token, 15)
    check(status == 200, f"expected 200 from GET /books/{{id}}/audio, got {status} {body}")
    audio = json.loads(body)
    url = audio.get("url")
    check(isinstance(url, str) and "X-Amz-Signature" in url, f"expected a SigV4 presigned url, got {url!r}")
    check(audio.get("durationMs", 0) > 0, f"expected a non-zero durationMs, got {audio.get('durationMs')!r}")
    check(audio.get("contentType") == "audio/mpeg", f"expected contentType audio/mpeg, got {audio.get('contentType')!r}")
    print(f"OK  GET {base}/audio -> 200 (presigned, expiresIn={audio.get('expiresIn')})")

    # THE assertion that proves section 3: the presign works with no
    # Authorization header at all -- which is the only reason an <audio>
    # element can play it, since a media request cannot carry one.
    status, headers, payload = _unauthed_get(url, 30)
    check(status == 200, f"expected 200 fetching the presigned URL WITHOUT auth, got {status}")
    check(
        headers.get("Content-Type") == "audio/mpeg",
        f"expected Content-Type audio/mpeg from S3, got {headers.get('Content-Type')!r}",
    )
    check(len(payload) > 0, "expected a non-empty body from the presigned URL")
    print(f"OK  presigned GET (no Authorization header) -> 200, {len(payload)} bytes of audio/mpeg")

    # THE load-bearing seeking assertion, and the one no unit test, mock or
    # fake can make: SigV4 query-string presigning signs only `host`, so
    # `Range` is unsigned and S3 honours whatever the browser sends. Chrome
    # issues `Range: bytes=N-` on every seek and expects a 206.
    status, headers, ranged = _unauthed_get(url, 30, {"Range": "bytes=0-1023"})
    check(status == 206, f"expected 206 for a Range request against the presigned URL, got {status}")
    check(len(ranged) == 1024, f"expected exactly 1024 bytes for bytes=0-1023, got {len(ranged)}")
    check("Content-Range" in headers, f"expected a Content-Range header on the 206, got {sorted(headers)}")
    check(ranged == payload[:1024], "the ranged bytes do not match the head of the full object")
    print(f"OK  presigned GET Range: bytes=0-1023 -> 206 ({headers.get('Content-Range')})")

    status, body = _authed_get(f"{base}/manifest", id_token, 15)
    check(status == 200, f"expected 200 from GET /books/{{id}}/manifest, got {status} {body}")
    manifest = json.loads(body)
    segments = manifest.get("segments", [])
    check(len(segments) >= 1, f"expected at least one segment with silent synthesis, got {len(segments)}")
    # Re-asserts phase-5 section 7.3's rounding contract THROUGH the API,
    # because that contract is exactly what phase 6's two-level binary search
    # rests on: segments must partition [0, durationMs) with no gap and no
    # overlap.
    expected_start = 0
    for segment in segments:
        check(
            segment["t"] == expected_start,
            f"segment {segment['i']} starts at {segment['t']}, expected {expected_start} (timeline not contiguous)",
        )
        expected_start = segment["t"] + segment["d"]
    check(
        expected_start == manifest.get("durationMs"),
        f"sum(segment.d) == {expected_start} but durationMs == {manifest.get('durationMs')}",
    )
    print(
        f"OK  GET {base}/manifest -> 200 ({len(segments)} contiguous segments, "
        f"durationMs={manifest.get('durationMs')})"
    )

    status, body = _authed_get(f"{base}/chunks/0/marks", id_token, 15)
    check(status == 200, f"expected 200 from GET /books/{{id}}/chunks/0/marks, got {status} {body}")
    marks = json.loads(body)
    words = marks.get("words", [])
    check(len(words) >= 1, f"expected a non-empty words array, got {len(words)}")
    previous = -1
    for word in words:
        check(word["t"] >= previous, f"marks are not sorted by non-decreasing t ({word['t']} < {previous})")
        previous = word["t"]
    check(
        marks.get("charStart") == segments[0]["s"],
        f"chunk 0 marks charStart {marks.get('charStart')!r} disagrees with the "
        f"manifest's segments[0].s {segments[0]['s']!r}",
    )
    print(f"OK  GET {base}/chunks/0/marks -> 200 ({len(words)} words, t non-decreasing)")


def check_resynthesize(
    api_url: str,
    book_id: str,
    id_token: str,
    expect_synthesis: str,
    stitch_timeout: float,
    *,
    synthesize_function: str = "",
    stitch_function: str = "",
) -> None:
    url = f"{api_url.rstrip('/')}/books/{book_id}/resynthesize"
    req = urllib.request.Request(url, method="POST", headers={"Authorization": f"Bearer {id_token}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, body = resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode("utf-8")

    if expect_synthesis == "silent":
        # The book is READY, so the PARTIAL-only guard must refuse it. Proves
        # the same guard from the other side that `failed` mode proves by
        # succeeding.
        check(status == 409, f"expected 409 from POST /resynthesize on a READY book, got {status} {body}")
        print(f"OK  POST {url} -> 409 (READY book, nothing to retry)")
        return

    check(status == 202, f"expected 202 from POST /resynthesize on a PARTIAL book, got {status} {body}")
    payload = json.loads(body)
    book = payload.get("book", {})
    chunks_total = book.get("progress", {}).get("chunksTotal", 0)
    check(
        payload.get("retriedChunks") == chunks_total,
        f"expected retriedChunks == chunksTotal == {chunks_total}, got {payload.get('retriedChunks')!r}",
    )
    check(book.get("status") == "EXTRACTED", f"expected the book to rewind to EXTRACTED, got {book.get('status')!r}")
    check(book.get("terminal") is False, f"expected terminal False after a rewind, got {book.get('terminal')!r}")
    print(
        f"OK  POST {url} -> 202 (retriedChunks={payload.get('retriedChunks')}, "
        f"book rewound to EXTRACTED)"
    )

    # The loop closes. This is what proves the fan-out was really
    # re-published and really consumed -- not merely that a DynamoDB row was
    # rewritten. Nothing else in this repo can prove /resynthesize end to end.
    # Pokes BOTH functions every iteration -- resynthesize republishes
    # straight to the synthesize queue (skipping extract), whose completing
    # increment then fans into the stitch queue, so either one might still
    # have work sitting unclaimed when the other finishes.
    final = _poll_book_terminal(
        api_url,
        book_id,
        id_token,
        stitch_timeout,
        invoke_functions=(synthesize_function, stitch_function),
    )
    check(
        final["status"] == "PARTIAL" and final.get("failureReason") == "NO_AUDIO",
        f"expected the retried book back at PARTIAL/NO_AUDIO, got "
        f"{final['status']}/{final.get('failureReason')!r}",
    )
    print(f"OK  retried book cycled back to terminal -> {final['status']}/{final.get('failureReason')}")


def _check_chunks_failed_with_tts_disabled(chunks: list) -> None:
    """`--expect-synthesis failed`: the raise-only StubSynthesizer, which is
    what every deployed non-prod stack runs."""
    for chunk in chunks:
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


def _check_chunks_synthesized_silently(chunks: list) -> None:
    """`--expect-synthesis silent`: the offline SilentSynthesizer (PLANS/
    phase-5.md OQ-1). Still zero network calls -- but real MPEG-2 bytes, so
    this is the only automated check that gives the stitcher something to
    concatenate."""
    for chunk in chunks:
        check(
            chunk["status"] == "DONE",
            f"chunk {chunk.get('index')}: expected DONE with the silent synthesizer, "
            f"got {chunk['status']} (failureReason={chunk.get('failureReason')!r})",
        )
        check(
            chunk.get("synthesisSource") == "silent",
            f"chunk {chunk.get('index')}: expected synthesisSource == 'silent', "
            f"got {chunk.get('synthesisSource')!r}",
        )
        for field in ("audioKey", "marksKey"):
            check(
                isinstance(chunk.get(field), str),
                f"chunk {chunk.get('index')}: expected {field} to be set, got {chunk.get(field)!r}",
            )
        check(
            chunk.get("durationMs", 0) > 0,
            f"chunk {chunk.get('index')}: expected a non-zero durationMs, got {chunk.get('durationMs')!r}",
        )


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    """Splits an SSE body into ``(event, data)`` pairs. Good enough for a
    trusted, fully-buffered response (this script reads the whole body, not
    incrementally) -- the streaming behaviour itself is asserted by
    e2e/chat.spec.ts's growing-textContent check (PLANS/phase-7.md §13.4),
    which this stdlib-only script cannot do."""
    frames: list[tuple[str, dict]] = []
    for raw_frame in body.split("\n\n"):
        event = None
        data = None
        for line in raw_frame.splitlines():
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = line[len("data:"):].strip()
        if event is not None and data is not None:
            frames.append((event, json.loads(data)))
    return frames


def _post_chat_question(
    chat_url: str, id_token: str | None, book_id: str, question: str, anchored_chunk: int = 0
) -> tuple[int, str]:
    headers = {"Content-Type": "application/json"}
    if id_token is not None:
        headers["Authorization"] = f"Bearer {id_token}"
    req = urllib.request.Request(
        f"{chat_url.rstrip('/')}/books/{book_id}/chat",
        method="POST",
        headers=headers,
        data=json.dumps({"question": question, "anchoredChunk": anchored_chunk}).encode("utf-8"),
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def check_chat(
    api_url: str,
    chat_url: str,
    cognito_endpoint: str,
    cognito_client_id: str,
    username: str,
    password: str,
    book_id: str,
) -> None:
    """PLANS/phase-7.md §12.2 -- runs in every environment this smoke test
    reaches, because the stub is present in all of them."""
    id_token = _login(cognito_endpoint, cognito_client_id, username, password)

    # 1. GET {api_url}/books/{id}/chat -> 200, no messages yet, chat disabled
    # with the stub's reason (this smoke test never runs against prod with a
    # real key -- deploy-prod.yml never calls this login-gated check at all,
    # same posture as every real external service since phase 4 §0).
    list_url = f"{api_url.rstrip('/')}/books/{book_id}/chat"
    status, body = _authed_get(list_url, id_token, 15)
    check(status == 200, f"expected 200 from GET {list_url}, got {status}: {body}")
    data = json.loads(body)
    check(data["messages"] == [], f"expected an empty transcript, got {data['messages']}")
    check(data["chat"]["enabled"] is False, f"expected chat.enabled is False, got {data['chat']}")
    check(
        data["chat"]["reason"] == "NON_PROD",
        f"expected chat.reason == 'NON_PROD', got {data['chat']['reason']!r}",
    )
    check(data["chat"]["dailyLimit"] == 50, f"expected dailyLimit == 50, got {data['chat']['dailyLimit']!r}")
    print(f"OK  GET {list_url} -> {status} (enabled=False, reason=NON_PROD, dailyLimit=50)")

    # 2. POST {chat_url}/books/{id}/chat with no Authorization -> 401. The
    # single assertion that proves in-Lambda JWT verification is wired at
    # all -- an unauthenticated POST to a NONE-auth Function URL reaching a
    # 200 would be the worst bug this phase could ship.
    status, _ = _post_chat_question(chat_url, None, book_id, "Smoke Test")
    check(status == 401, f"expected 401 for unauthenticated POST to {chat_url}, got {status}")
    print(f"OK  POST {chat_url}/books/{book_id}/chat (no token) -> {status}")

    # 3. OPTIONS preflight -> 2xx with access-control-allow-origin present,
    # and specifically NOT 401 (phase-6 §16's bug, measured not reasoned
    # about).
    preflight_url = f"{chat_url.rstrip('/')}/books/{book_id}/chat"
    req = urllib.request.Request(
        preflight_url,
        method="OPTIONS",
        headers={
            "Origin": "https://example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            preflight_status = resp.status
            preflight_headers = dict(resp.headers)
    except urllib.error.HTTPError as exc:
        preflight_status = exc.code
        preflight_headers = dict(exc.headers or {})
    check(
        200 <= preflight_status < 300,
        f"expected a 2xx CORS preflight from {preflight_url}, got {preflight_status}",
    )
    check(
        any(k.lower() == "access-control-allow-origin" for k in preflight_headers),
        f"expected access-control-allow-origin on the preflight response, got headers {preflight_headers}",
    )
    print(f"OK  OPTIONS {preflight_url} -> {preflight_status} (CORS preflight answered without invoking the function)")

    # 4. POST with a token belonging to another user's book id -> 404, never
    # 403 (PLANS/phase-7.md §7.3's authorization test, at the deployed
    # layer).
    status, body = _post_chat_question(chat_url, id_token, "not-my-book-id", "Smoke Test")
    check(status == 404, f"expected 404 for a foreign/missing book id, got {status}: {body}")
    print(f"OK  POST {chat_url}/books/not-my-book-id/chat -> {status}")

    # 5. POST with a valid token -> 200, text/event-stream, frame by frame.
    status, chunks_body = _authed_get(f"{api_url.rstrip('/')}/books/{book_id}/chunks", id_token, 15)
    check(status == 200, f"expected 200 from GET .../chunks, got {status}: {chunks_body}")
    chunks = json.loads(chunks_body)
    check(len(chunks) > 0, "expected at least one chunk to anchor the chat question on")
    anchor_text = chunks[0]["text"]

    status, body = _post_chat_question(chat_url, id_token, book_id, "What is this section about?", anchored_chunk=0)
    check(status == 200, f"expected 200 from POST {chat_url}/books/{book_id}/chat, got {status}: {body}")
    frames = _parse_sse(body)
    check(len(frames) > 0, "expected at least one SSE frame")

    first_event, first_data = frames[0]
    check(first_event == "meta", f"expected the first frame to be 'meta', got {first_event!r}")
    check(first_data["enabled"] is False, f"expected meta.enabled is False, got {first_data}")
    check(first_data["model"] == "stub", f"expected meta.model == 'stub', got {first_data.get('model')!r}")
    check(
        first_data["anchorChunk"] == 0,
        f"expected meta.anchorChunk == 0, got {first_data.get('anchorChunk')!r}",
    )

    delta_frames = [d for e, d in frames if e == "delta"]
    check(
        len(delta_frames) >= 3,
        f"expected at least 3 delta frames (a real stream, not one blob), got {len(delta_frames)}",
    )

    last_event, last_data = frames[-1]
    check(last_event == "done", f"expected the last frame to be 'done', got {last_event!r}")
    check(
        last_data["finishReason"] == "DISABLED",
        f"expected done.finishReason == 'DISABLED', got {last_data.get('finishReason')!r}",
    )

    concatenated = "".join(d["text"] for d in delta_frames)
    marker = anchor_text[:40]
    check(
        marker in concatenated,
        f"expected the answer to contain the anchor chunk's opening text {marker!r}; got {concatenated!r}",
    )
    print(
        f"OK  POST {chat_url}/books/{book_id}/chat -> {status} "
        f"(meta -> {len(delta_frames)} deltas -> done, answer references book content)"
    )

    # 6. A second POST -> both turns appear in GET .../chat, ordered
    # oldest-first, roles alternating.
    status, body = _post_chat_question(chat_url, id_token, book_id, "A follow-up question?", anchored_chunk=0)
    check(status == 200, f"expected 200 from the second POST, got {status}: {body}")

    status, body = _authed_get(list_url, id_token, 15)
    check(status == 200, f"expected 200 from GET {list_url}, got {status}: {body}")
    messages = json.loads(body)["messages"]
    check(len(messages) == 4, f"expected 4 messages after two questions, got {len(messages)}")
    check(
        [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"],
        f"expected alternating user/assistant roles, got {[m['role'] for m in messages]}",
    )
    check(
        messages == sorted(messages, key=lambda m: m["createdAt"]),
        "expected messages ordered oldest-first by createdAt",
    )
    print(f"OK  GET {list_url} -> {status} (4 messages, alternating roles, oldest-first)")

    # 7. DELETE {api_url}/books/{id}/chat -> 200 {"deleted": 4}, then GET ->
    # empty.
    del_req = urllib.request.Request(
        list_url, method="DELETE", headers={"Authorization": f"Bearer {id_token}"}
    )
    with urllib.request.urlopen(del_req, timeout=15) as resp:
        del_status = resp.status
        del_body = json.loads(resp.read().decode("utf-8"))
    check(del_status == 200, f"expected 200 from DELETE {list_url}, got {del_status}")
    check(
        del_body.get("deleted") == 4,
        f"expected {{'deleted': 4}} from DELETE {list_url}, got {del_body}",
    )
    status, body = _authed_get(list_url, id_token, 15)
    check(json.loads(body)["messages"] == [], "expected an empty transcript after DELETE")
    print(f"OK  DELETE {list_url} -> {del_status} ({del_body}); transcript now empty")

    # 8. A 3,000-character question -> 422.
    status, body = _post_chat_question(chat_url, id_token, book_id, "x" * 3000)
    check(status == 422, f"expected 422 for an over-long question, got {status}: {body}")
    print(f"OK  POST {chat_url}/books/{book_id}/chat (3000-char question) -> {status}")

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
    parser.add_argument("--chat-url", default=None)
    parser.add_argument("--frontend-url", default=None)
    parser.add_argument("--expect-commit", default=None)
    parser.add_argument("--expect-environment", default=None)
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--extraction-timeout", type=float, default=120)
    parser.add_argument("--synthesis-timeout", type=float, default=180)
    parser.add_argument("--stitch-timeout", type=float, default=120)
    parser.add_argument("--expect-synthesis", choices=["failed", "silent"], default="failed")
    parser.add_argument("--skip-synthesis", action="store_true")
    parser.add_argument("--cognito-endpoint", default=None)
    parser.add_argument("--cognito-region", default=None)
    parser.add_argument("--cognito-client-id", default=None)
    parser.add_argument("--auth-signup", action="store_true")
    parser.add_argument("--login-username", default=None)
    parser.add_argument("--login-password", default=None)
    parser.add_argument("--newuser-username", default=None)
    parser.add_argument("--newuser-temp-password", default=None)
    # Deployed Lambda function names (empty by default -- local-smoke's
    # LocalStack workers poll every second and never need this). Real AWS
    # runs pass these so the upload/synthesis/stitch polls above can invoke
    # each pipeline Lambda directly instead of waiting on its own
    # EventBridge Rule schedule (up to 20 minutes on stitch) -- see
    # check_upload_and_stitch's docstring.
    parser.add_argument("--extract-function-name", default="")
    parser.add_argument("--synthesize-function-name", default="")
    parser.add_argument("--stitch-function-name", default="")
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
            book_id = check_upload_and_stitch(
                args.api_url,
                cognito_endpoint,
                args.cognito_client_id,
                args.login_username,
                args.login_password,
                args.extraction_timeout,
                args.synthesis_timeout,
                args.stitch_timeout,
                skip_synthesis=args.skip_synthesis,
                expect_synthesis=args.expect_synthesis,
                extract_function=args.extract_function_name,
                synthesize_function=args.synthesize_function_name,
                stitch_function=args.stitch_function_name,
            )
            if args.chat_url:
                check_chat(
                    args.api_url,
                    args.chat_url,
                    cognito_endpoint,
                    args.cognito_client_id,
                    args.login_username,
                    args.login_password,
                    book_id,
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
