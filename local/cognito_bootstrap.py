"""Create the local Cognito user pool the app expects.

Runs inside the backend container (it already has boto3):

    docker compose exec -T backend python /local-shared/cognito_bootstrap.py

Kept out of backend/src/ on purpose: it is dev tooling, and src/ is
coverage-gated. Idempotent, modelled on cashlytics's src/auth/bootstrap.py --
adapted for a username-only, self-sign-up pool (bookloud has no admin group,
no admin-created users).

1. get-or-create user pool "bookloud-local" -- no UsernameAttributes/
   AliasAttributes, so sign-in is username-only, matching the real pool;
   relaxed password policy (min 8, no classes), matching the non-prod CDK
   policy.
2. get-or-create app client "WebClient" with
   ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
   no secret.
3. get-or-create user "dev" via admin_create_user(MessageAction="SUPPRESS")
   + admin_set_user_password(Password="password", Permanent=True) -- a
   confirmed, known-good login for `make smoke` and manual dev.
4. get-or-create user "newuser" via admin_create_user(MessageAction="SUPPRESS")
   + admin_set_user_password(Password="TempPass123!", Permanent=False) --
   a temporary, non-permanent password so the NEW_PASSWORD_REQUIRED
   first-login challenge (PLANS/phase-1.md §11, admin-only provisioning)
   has something to exercise locally.
5. write /local-shared/.cognito.env.
"""

from __future__ import annotations

import os

import boto3

POOL_NAME = "bookloud-local"
CLIENT_NAME = "WebClient"
DEV_USERNAME = "dev"
DEV_PASSWORD = "password"
# Admin-provisioned user with a temporary (non-permanent) password, so the
# NEW_PASSWORD_REQUIRED challenge has something to exercise locally.
NEWUSER_USERNAME = "newuser"
NEWUSER_TEMP_PASSWORD = "TempPass123!"
REGION = os.environ.get("COGNITO_REGION", "us-east-1")
OUTPUT_FILE = "/local-shared/.cognito.env"


def _client():
    endpoint = os.environ.get("COGNITO_ENDPOINT_URL")
    return boto3.client(
        "cognito-idp",
        region_name=REGION,
        endpoint_url=endpoint,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID", "local"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY", "local"),
    )


def _get_or_create_pool(client) -> str:
    for pool in client.list_user_pools(MaxResults=60)["UserPools"]:
        if pool["Name"] == POOL_NAME:
            return pool["Id"]
    created = client.create_user_pool(
        PoolName=POOL_NAME,
        Policies={
            "PasswordPolicy": {
                "MinimumLength": 8,
                "RequireUppercase": False,
                "RequireLowercase": False,
                "RequireNumbers": False,
                "RequireSymbols": False,
            }
        },
    )
    return created["UserPool"]["Id"]


def _get_or_create_client(client, pool_id: str) -> str:
    for app_client in client.list_user_pool_clients(
        UserPoolId=pool_id, MaxResults=60
    )["UserPoolClients"]:
        if app_client["ClientName"] == CLIENT_NAME:
            return app_client["ClientId"]
    return client.create_user_pool_client(
        UserPoolId=pool_id,
        ClientName=CLIENT_NAME,
        ExplicitAuthFlows=["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"],
    )["UserPoolClient"]["ClientId"]


def _ensure_dev_user(client, pool_id: str) -> None:
    try:
        client.admin_create_user(
            UserPoolId=pool_id,
            Username=DEV_USERNAME,
            MessageAction="SUPPRESS",
        )
    except client.exceptions.UsernameExistsException:
        pass
    client.admin_set_user_password(
        UserPoolId=pool_id,
        Username=DEV_USERNAME,
        Password=DEV_PASSWORD,
        Permanent=True,
    )


def _ensure_newuser(client, pool_id: str) -> None:
    """Admin-provisioned user, temporary password -- exercises the
    NEW_PASSWORD_REQUIRED first-login challenge locally. Re-running this
    (idempotent bootstrap) re-sets the temporary password each time, so the
    challenge is always exercisable even after a previous local/smoke_test.py
    run already completed it once."""
    try:
        client.admin_create_user(
            UserPoolId=pool_id,
            Username=NEWUSER_USERNAME,
            MessageAction="SUPPRESS",
        )
    except client.exceptions.UsernameExistsException:
        pass
    client.admin_set_user_password(
        UserPoolId=pool_id,
        Username=NEWUSER_USERNAME,
        Password=NEWUSER_TEMP_PASSWORD,
        Permanent=False,
    )


def bootstrap() -> tuple[str, str]:
    """Ensure the pool/client/dev-user/newuser exist; return (pool_id, client_id)."""
    client = _client()
    pool_id = _get_or_create_pool(client)
    client_id = _get_or_create_client(client, pool_id)
    _ensure_dev_user(client, pool_id)
    _ensure_newuser(client, pool_id)
    return pool_id, client_id


def main() -> None:
    pool_id, client_id = bootstrap()

    with open(OUTPUT_FILE, "w") as f:
        f.write(f"COGNITO_CLIENT_ID={client_id}\n")
        f.write(f"COGNITO_USER_POOL_ID={pool_id}\n")
        f.write(f"COGNITO_REGION={REGION}\n")

    print(f"Cognito ready: pool={pool_id} client={client_id}")
    print(f"dev user: {DEV_USERNAME} / {DEV_PASSWORD}")
    print(
        f"newuser (temp password, forces NEW_PASSWORD_REQUIRED): "
        f"{NEWUSER_USERNAME} / {NEWUSER_TEMP_PASSWORD}"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
