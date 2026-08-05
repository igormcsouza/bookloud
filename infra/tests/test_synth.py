"""``aws_cdk.assertions`` coverage over every stack, for `environment="pr-99"`
and `environment="prod"`. Runs with no AWS credentials.

Note: ``DockerImageCode.from_image_asset`` (used by ApiStack, and therefore
by the full `infra/app.py` wiring) requires a working Docker daemon at synth
time to build the backend image. Those tests are marked ``@pytest.mark.docker``
so the rest of this suite still runs in environments without Docker; run
`pytest -m "not docker"` to skip them.
"""

from __future__ import annotations

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Match, Template

from stacks.auth_stack import AuthStack
from stacks.frontend_stack import FrontendStack
from stacks.pipeline_stack import PipelineStack
from stacks.storage_stack import StorageStack

ENVIRONMENTS = ["pr-99", "prod"]


def _synth(stack_cls, environment: str, **kwargs) -> Template:
    app = cdk.App()
    stack = stack_cls(
        app,
        f"Test{stack_cls.__name__}-{environment}",
        environment=environment,
        **kwargs,
    )
    return Template.from_stack(stack)


# --- StorageStack --------------------------------------------------------


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_storage_stack_synthesizes(environment: str) -> None:
    template = _synth(StorageStack, environment)
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.resource_count_is("AWS::S3::Bucket", 3)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_storage_stack_table_key_schema(environment: str) -> None:
    template = _synth(StorageStack, environment)
    template.has_resource_properties(
        "AWS::DynamoDB::Table",
        {
            "KeySchema": Match.array_with(
                [
                    {"AttributeName": "PK", "KeyType": "HASH"},
                    {"AttributeName": "SK", "KeyType": "RANGE"},
                ]
            )
        },
    )


def test_storage_stack_prod_retains() -> None:
    template = _synth(StorageStack, "prod")
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Retain"})
    for resource in template.find_resources("AWS::S3::Bucket").values():
        assert resource["DeletionPolicy"] == "Retain"


def test_storage_stack_pr_deletes() -> None:
    template = _synth(StorageStack, "pr-99")
    template.has_resource("AWS::DynamoDB::Table", {"DeletionPolicy": "Delete"})
    for resource in template.find_resources("AWS::S3::Bucket").values():
        assert resource["DeletionPolicy"] == "Delete"


# --- AuthStack -------------------------------------------------------------


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_auth_stack_synthesizes(environment: str) -> None:
    template = _synth(AuthStack, environment)
    template.resource_count_is("AWS::Cognito::UserPool", 1)
    template.resource_count_is("AWS::Cognito::UserPoolClient", 1)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_auth_stack_username_only_sign_in(environment: str) -> None:
    """The assertion that protects the immutable Cognito sign-in setting:
    UsernameAttributes/AliasAttributes must be absent, which is how CDK/
    CloudFormation express "username, not email or phone, is the sign-in
    identifier"."""
    template = _synth(AuthStack, environment)
    pools = template.find_resources("AWS::Cognito::UserPool")
    assert len(pools) == 1
    (props,) = [r["Properties"] for r in pools.values()]
    assert "UsernameAttributes" not in props
    assert "AliasAttributes" not in props


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_auth_stack_self_sign_up_disabled(environment: str) -> None:
    """PLANS/phase-1.md §11: user provisioning is admin-only now, not
    self-signup. self_sign_up_enabled=False synthesizes as
    AdminCreateUserConfig.AllowAdminCreateUserOnly=True -- this is the real
    enforcement (Cognito rejects SignUp regardless of what the frontend
    does). Inverts the phase-1 assumption that self-signup was enabled."""
    template = _synth(AuthStack, environment)
    pools = template.find_resources("AWS::Cognito::UserPool")
    (props,) = [r["Properties"] for r in pools.values()]
    assert props["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"] is True


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_auth_stack_has_presignup_trigger(environment: str) -> None:
    """PreSignUp auto-confirm trigger: the pool has no auto-verified
    attributes, so a self-signed-up user needs a deterministic, email-free
    way out of UNCONFIRMED."""
    template = _synth(AuthStack, environment)
    pools = template.find_resources("AWS::Cognito::UserPool")
    (props,) = [r["Properties"] for r in pools.values()]
    assert "LambdaConfig" in props
    assert "PreSignUp" in props["LambdaConfig"]
    # Exactly one Lambda in this stack: the PreSignUp trigger itself.
    template.resource_count_is("AWS::Lambda::Function", 1)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_auth_stack_client_explicit_auth_flows(environment: str) -> None:
    """CDK appends ALLOW_REFRESH_TOKEN_AUTH automatically alongside the
    USER_PASSWORD_AUTH flow we request -- assert it explicitly so a CDK
    upgrade can't silently break the refresh flow the frontend BFF depends
    on."""
    template = _synth(AuthStack, environment)
    clients = template.find_resources("AWS::Cognito::UserPoolClient")
    (props,) = [r["Properties"] for r in clients.values()]
    flows = set(props["ExplicitAuthFlows"])
    assert "ALLOW_USER_PASSWORD_AUTH" in flows
    assert "ALLOW_REFRESH_TOKEN_AUTH" in flows


# --- PipelineStack -----------------------------------------------------
#
# PipelineStack now builds a DockerImageFunction (the extract Lambda, phase
# 3), so every synth here requires a Docker daemon -- see this module's
# docstring. `_synth_pipeline_stack` builds a StorageStack first (the extract
# Lambda's re-imported pdf_bucket + table come from it).


def _synth_pipeline_stack(environment: str) -> Template:
    app = cdk.App()
    storage = StorageStack(app, f"TestStorageForPipeline-{environment}", environment=environment)
    pipeline = PipelineStack(
        app,
        f"TestPipeline-{environment}",
        environment=environment,
        pdf_bucket_name=storage.pdf_bucket.bucket_name,
        table=storage.table,
        git_sha="test-sha",
    )
    return Template.from_stack(pipeline)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_synthesizes(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.resource_count_is("AWS::SQS::Queue", 4)
    # The extract Lambda + the (auto-created, inline-Python, no-Docker)
    # BucketNotificationsHandler singleton that an *imported* bucket's
    # add_event_notification synthesizes.
    template.resource_count_is("AWS::Lambda::Function", 2)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_has_redrive_policies(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    queues_with_redrive = template.find_resources(
        "AWS::SQS::Queue", {"Properties": {"RedrivePolicy": Match.any_value()}}
    )
    # extract + synthesize each have a redrive policy pointing at their DLQ;
    # the two DLQs themselves do not.
    assert len(queues_with_redrive) == 2


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_extract_queue_visibility_timeout(environment: str) -> None:
    """6x the extract Lambda's 120s timeout (AWS's guidance) -- a shorter
    visibility timeout would cause duplicate concurrent invocations on the
    same message."""
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": Match.string_like_regexp("^bookloud-.*-extract$"), "VisibilityTimeout": 720},
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_has_s3_bucket_notification(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.resource_count_is("Custom::S3BucketNotifications", 1)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_queue_policy_allows_s3_to_send(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    policies = template.find_resources("AWS::SQS::QueuePolicy")
    assert len(policies) == 1
    (props,) = [p["Properties"] for p in policies.values()]
    principals = [
        stmt["Principal"].get("Service")
        for stmt in props["PolicyDocument"]["Statement"]
        if isinstance(stmt.get("Principal"), dict)
    ]
    assert "s3.amazonaws.com" in principals


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_extract_lambda_event_source_mapping(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 1)
    template.has_resource_properties("AWS::Lambda::EventSourceMapping", {"BatchSize": 1})


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_extract_lambda_role_grants(environment: str) -> None:
    """Regression guard on `pdf_bucket.grant_read(extract_fn)` +
    `table.grant_read_write_data(extract_fn)` -- the extract Lambda actually
    depends on GetObject (fetch the source PDF) and UpdateItem (the
    EXTRACTING claim / status flip via BookRepository.update_status)."""
    template = _synth_pipeline_stack(environment)

    policies = template.find_resources("AWS::IAM::Policy")
    actions: list[str] = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)

    assert "s3:GetObject*" in actions or "s3:GetObject" in actions
    assert "dynamodb:UpdateItem" in actions


# --- FrontendStack -------------------------------------------------------


_FRONTEND_KWARGS = dict(
    api_base_url="https://api.example.com",
    cognito_client_id="test-client-id",
    cognito_region="us-east-1",
)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_frontend_stack_synthesizes_without_a_build(environment: str) -> None:
    """No `.open-next` build exists in this checkout -> the placeholder Lambda
    code path is exercised, proving synth never hard-depends on a frontend
    build."""
    template = _synth(FrontendStack, environment, **_FRONTEND_KWARGS)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_frontend_stack_has_two_cache_behaviours(environment: str) -> None:
    template = _synth(FrontendStack, environment, **_FRONTEND_KWARGS)
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    assert "DefaultCacheBehavior" in config
    assert len(config["CacheBehaviors"]) == 1


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_frontend_stack_has_cognito_client_id_env(environment: str) -> None:
    """The SSR Lambda's BFF route handlers (frontend/lib/cognito.ts) need
    COGNITO_CLIENT_ID at runtime, not build time -- no NEXT_PUBLIC_* value."""
    template = _synth(FrontendStack, environment, **_FRONTEND_KWARGS)
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {"Properties": {"Environment": {"Variables": Match.object_like({"BUCKET_NAME": Match.any_value()})}}},
    )
    (props,) = [r["Properties"] for r in functions.values()]
    assert props["Environment"]["Variables"]["COGNITO_CLIENT_ID"] == "test-client-id"


# --- Stack naming ----------------------------------------------------------


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_naming_convention(environment: str) -> None:
    # PipelineStack now requires Docker (it builds the extract Lambda's
    # image), so it's out of the generic, Docker-free naming test below and
    # gets its own docker-marked version instead.
    app = cdk.App()
    construct_id = f"BookloudPipeline-{environment}"
    storage = StorageStack(app, f"TestStorageForNaming-{environment}", environment=environment)
    stack = PipelineStack(
        app,
        construct_id,
        environment=environment,
        pdf_bucket_name=storage.pdf_bucket.bucket_name,
        table=storage.table,
    )
    assert stack.stack_name == construct_id


@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize(
    "stack_cls,component",
    [
        (StorageStack, "Storage"),
        (AuthStack, "Auth"),
    ],
)
def test_stack_naming_convention(stack_cls, component, environment: str) -> None:
    app = cdk.App()
    construct_id = f"Bookloud{component}-{environment}"
    stack = stack_cls(app, construct_id, environment=environment)
    assert stack.stack_name == construct_id


# --- ApiStack + full app wiring (requires Docker) ---------------------------


def _synth_api_stack(environment: str):
    from stacks.api_stack import ApiStack

    app = cdk.App()
    storage = StorageStack(app, f"TestStorage-{environment}", environment=environment)
    pipeline = PipelineStack(
        app,
        f"TestPipeline-{environment}",
        environment=environment,
        pdf_bucket_name=storage.pdf_bucket.bucket_name,
        table=storage.table,
    )
    auth = AuthStack(app, f"TestAuth-{environment}", environment=environment)
    api = ApiStack(
        app,
        f"TestApi-{environment}",
        table=storage.table,
        pdf_bucket=storage.pdf_bucket,
        audio_bucket=storage.audio_bucket,
        marks_bucket=storage.marks_bucket,
        extract_queue=pipeline.extract_queue,
        user_pool=auth.user_pool,
        user_pool_client=auth.user_pool_client,
        environment=environment,
        git_sha="test-sha",
    )
    return Template.from_stack(api)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_synthesizes(environment: str) -> None:
    template = _synth_api_stack(environment)
    # 1 API Lambda + 1 PreSignUp trigger Lambda (from the nested AuthStack
    # construct tree -- but AuthStack is a separate stack, so only the API
    # Lambda lands in *this* stack's template).
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert any("/health" in key for key in route_keys)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_jwt_authorizer(environment: str) -> None:
    """This is the regression test for the whole phase -- it is what proves
    "protected route rejection without token" at the infra layer."""
    template = _synth_api_stack(environment)

    authorizers = template.find_resources("AWS::ApiGatewayV2::Authorizer")
    assert len(authorizers) == 1
    (authorizer_props,) = [r["Properties"] for r in authorizers.values()]
    assert authorizer_props["AuthorizerType"] == "JWT"

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    by_key = {r["Properties"]["RouteKey"]: r["Properties"] for r in routes.values()}

    proxy_route = by_key["ANY /{proxy+}"] if "ANY /{proxy+}" in by_key else next(
        props for key, props in by_key.items() if "/{proxy+}" in key
    )
    assert proxy_route["AuthorizationType"] == "JWT"
    assert "AuthorizerId" in proxy_route

    health_route = next(props for key, props in by_key.items() if "/health" in key)
    assert health_route["AuthorizationType"] == "NONE"


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_lambda_role_grants_dynamodb_query(environment: str) -> None:
    """PLANS/phase-2.md §8: regression guard on the `table.grant_read_write_data(fn)`
    grant Phase 2's Library context repositories now actually depend on
    (``Query`` backs ``list_for_user``/``list_for_book``). CDK's
    ``grant_read_write_data`` bundles the IAM actions into one or more
    ``AWS::IAM::Policy`` resources attached to the function's role; find them
    and assert `dynamodb:Query` appears in at least one statement's Action
    list."""
    template = _synth_api_stack(environment)

    policies = template.find_resources("AWS::IAM::Policy")
    actions: list[str] = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)

    assert "dynamodb:Query" in actions
