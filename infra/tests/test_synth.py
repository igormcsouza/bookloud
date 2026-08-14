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


def _synth_pipeline_stack(environment: str, *, google_tts_secret_name: str = "") -> Template:
    app = cdk.App()
    storage = StorageStack(app, f"TestStorageForPipeline-{environment}", environment=environment)
    pipeline = PipelineStack(
        app,
        f"TestPipeline-{environment}",
        environment=environment,
        pdf_bucket_name=storage.pdf_bucket.bucket_name,
        audio_bucket=storage.audio_bucket,
        marks_bucket=storage.marks_bucket,
        table=storage.table,
        google_tts_secret_name=google_tts_secret_name,
        git_sha="test-sha",
    )
    return Template.from_stack(pipeline)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_synthesizes(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    # extract + synthesize + stitch, each with a DLQ.
    template.resource_count_is("AWS::SQS::Queue", 6)
    # The extract Lambda + the synthesize Lambda + the stitch Lambda + the
    # (auto-created, inline-Python, no-Docker) BucketNotificationsHandler
    # singleton that an *imported* bucket's add_event_notification
    # synthesizes.
    template.resource_count_is("AWS::Lambda::Function", 4)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_has_redrive_policies(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    queues_with_redrive = template.find_resources(
        "AWS::SQS::Queue", {"Properties": {"RedrivePolicy": Match.any_value()}}
    )
    # extract + synthesize + stitch each have a redrive policy pointing at
    # their DLQ; the three DLQs themselves do not.
    assert len(queues_with_redrive) == 3


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
def test_pipeline_stack_synthesize_queue_shape(environment: str) -> None:
    """PLANS/phase-4.md §6.2: 30 min visibility (6x the synthesize Lambda's
    300s timeout) and max_receive_count 5 (not 3) -- throttle-induced
    redeliveries would otherwise burn the retry budget."""
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": Match.string_like_regexp("^bookloud-.*-synthesize$"), "VisibilityTimeout": 1800},
    )
    queues = template.find_resources(
        "AWS::SQS::Queue", {"Properties": {"QueueName": Match.string_like_regexp("^bookloud-.*-synthesize$")}}
    )
    (props,) = [r["Properties"] for r in queues.values()]
    dlq_ref = props["RedrivePolicy"]["deadLetterTargetArn"]
    assert props["RedrivePolicy"]["maxReceiveCount"] == 5
    assert dlq_ref is not None

    # extract queue is unchanged: 12 min / 3.
    extract_queues = template.find_resources(
        "AWS::SQS::Queue", {"Properties": {"QueueName": Match.string_like_regexp("^bookloud-.*-extract$")}}
    )
    (extract_props,) = [r["Properties"] for r in extract_queues.values()]
    assert extract_props["RedrivePolicy"]["maxReceiveCount"] == 3


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
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 3)
    template.has_resource_properties("AWS::Lambda::EventSourceMapping", {"BatchSize": 1})


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_synthesize_lambda_event_source_mapping(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        # 3, lowered from 5 in phase 5 (PLANS/phase-5.md OQ-2): the account's
        # total Lambda concurrency is 10 and the stitch function now competes
        # for it, so 5 here could starve the user-facing API during
        # processing.
        {"BatchSize": 1, "ScalingConfig": {"MaximumConcurrency": 2}},
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_synthesize_function_shape(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "ImageConfig": {
                "Command": ["src.contexts.library.interface.synthesize_handler.handler"]
            },
            # Absent, not 10. This account's total Lambda concurrency limit
            # is 10 and AWS rejects any reservation that drops unreserved
            # concurrency below its floor of 10, so setting this at all fails
            # the deploy (see the comment in pipeline_stack.py). Asserting
            # absence turns a re-added reservation into a failing unit test
            # rather than a CREATE_FAILED six minutes into deploy-pr.
            "ReservedConcurrentExecutions": Match.absent(),
            "Timeout": 300,
            "MemorySize": 1024,
        },
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_stitch_queue_shape(environment: str) -> None:
    """PLANS/phase-5.md §6.2: 90 min visibility (6x the stitch Lambda's 900s
    timeout, the repo's standing rule) and max_receive_count 3 -- unlike the
    synthesize queue there is no ESM-throttle backpressure to burn attempts,
    and each attempt costs up to 15 minutes."""
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::SQS::Queue",
        {"QueueName": Match.string_like_regexp("^bookloud-.*-stitch$"), "VisibilityTimeout": 5400},
    )
    queues = template.find_resources(
        "AWS::SQS::Queue",
        {"Properties": {"QueueName": Match.string_like_regexp("^bookloud-.*-stitch$")}},
    )
    (props,) = [r["Properties"] for r in queues.values()]
    assert props["RedrivePolicy"]["maxReceiveCount"] == 3
    assert props["RedrivePolicy"]["deadLetterTargetArn"] is not None

    # The other two queues are unchanged: synthesize 30 min / 5, extract
    # 12 min / 3.
    for name, timeout, receives in (("synthesize", 1800, 5), ("extract", 720, 3)):
        found = template.find_resources(
            "AWS::SQS::Queue",
            {"Properties": {"QueueName": Match.string_like_regexp(f"^bookloud-.*-{name}$")}},
        )
        (found_props,) = [r["Properties"] for r in found.values()]
        assert found_props["VisibilityTimeout"] == timeout
        assert found_props["RedrivePolicy"]["maxReceiveCount"] == receives


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_stitch_function_shape(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "ImageConfig": {"Command": ["src.contexts.library.interface.stitch_handler.handler"]},
            # Absent, not a number. Same account-quota wall as
            # SynthesizeFunction: this account's total Lambda concurrency
            # limit is 10 and AWS rejects any reservation that drops
            # unreserved concurrency below its floor of 10, so setting this
            # at all fails the deploy. Asserting absence turns a re-added
            # reservation into a failing unit test rather than a
            # CREATE_FAILED six minutes into deploy-pr.
            "ReservedConcurrentExecutions": Match.absent(),
            "Timeout": 900,
            "MemorySize": 1536,
        },
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_stitch_lambda_event_source_mapping(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::Lambda::EventSourceMapping",
        {"BatchSize": 1, "ScalingConfig": {"MaximumConcurrency": 2}},
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_stitch_function_has_the_queue_url_and_buckets(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": {
                "ImageConfig": {
                    "Command": ["src.contexts.library.interface.stitch_handler.handler"]
                }
            }
        },
    )
    (props,) = [r["Properties"] for r in functions.values()]
    variables = props["Environment"]["Variables"]
    assert "STITCH_QUEUE_URL" in variables
    assert variables["STITCH_MAX_RECEIVE_COUNT"] == "3"
    assert "AUDIO_BUCKET" in variables
    assert "MARKS_BUCKET" in variables
    # The stitch Lambda never calls a TTS engine, so it carries none of the
    # synthesis config -- in particular no GOOGLE_TTS_SECRET_NAME.
    assert "GOOGLE_TTS_SECRET_NAME" not in variables


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_synthesize_function_carries_the_stitch_queue_url(environment: str) -> None:
    """The fan-in producer's env var, the twin of the grant asserted below."""
    template = _synth_pipeline_stack(environment)
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": {
                "ImageConfig": {
                    "Command": ["src.contexts.library.interface.synthesize_handler.handler"]
                }
            }
        },
    )
    (props,) = [r["Properties"] for r in functions.values()]
    assert "STITCH_QUEUE_URL" in props["Environment"]["Variables"]


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


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_synthesize_lambda_and_fan_out_grants(environment: str) -> None:
    """Regression guard on the synthesize Lambda's audio/marks `grant_put`
    calls, on the fan-out producer grant
    (`synthesize_queue.grant_send_messages(extract_fn)`), and on phase 5's
    fan-in twin (`stitch_queue.grant_send_messages(synthesize_fn)`) -- the
    two easy-to-forget grants, since both point the "wrong" direction."""
    template = _synth_pipeline_stack(environment)

    policies = template.find_resources("AWS::IAM::Policy")
    actions: list[str] = []
    send_message_statements = 0
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)
            if "sqs:SendMessage" in (action if isinstance(action, list) else [action]):
                send_message_statements += 1

    assert "s3:PutObject" in actions
    assert "sqs:SendMessage" in actions
    # TWO producer grants, asserted by count so losing either one fails:
    # extract -> synthesize_queue, and synthesize -> stitch_queue.
    assert send_message_statements == 2
    # The stitch Lambda is the first function that READS audio_bucket -- the
    # synthesize Lambda deliberately only has grant_put.
    assert "s3:GetObject*" in actions or "s3:GetObject" in actions
    # grant_put covers the multipart upload's Abort permission.
    assert any(a.startswith("s3:Abort") for a in actions)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_google_secret_configured_grants_secretsmanager_read(environment: str) -> None:
    template = _synth_pipeline_stack(environment, google_tts_secret_name="test-secret")

    policies = template.find_resources("AWS::IAM::Policy")
    actions: list[str] = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)

    assert "secretsmanager:GetSecretValue" in actions
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Environment": {
                "Variables": Match.object_like({"GOOGLE_TTS_SECRET_NAME": "test-secret"})
            }
        },
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_no_google_secret_no_secretsmanager_actions_anywhere(environment: str) -> None:
    """Negative test: with google_tts_secret_name="" (the default), no
    secretsmanager:* action appears anywhere in the stack."""
    template = _synth_pipeline_stack(environment, google_tts_secret_name="")

    policies = template.find_resources("AWS::IAM::Policy")
    actions: list[str] = []
    for policy in policies.values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)

    assert not any(a.startswith("secretsmanager:") for a in actions)


# --- FrontendStack -------------------------------------------------------


_FRONTEND_KWARGS = {
    "api_base_url": "https://api.example.com",
    "chat_base_url": "https://chat.example.com",
    "cognito_client_id": "test-client-id",
    "cognito_region": "us-east-1",
}


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
        audio_bucket=storage.audio_bucket,
        marks_bucket=storage.marks_bucket,
        table=storage.table,
    )
    assert stack.stack_name == construct_id

    queue_names = {
        r["Properties"]["QueueName"]
        for r in Template.from_stack(stack).find_resources("AWS::SQS::Queue").values()
    }
    assert f"bookloud-{environment}-stitch" in queue_names
    assert f"bookloud-{environment}-stitch-dlq" in queue_names


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
        audio_bucket=storage.audio_bucket,
        marks_bucket=storage.marks_bucket,
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
        synthesize_queue=pipeline.synthesize_queue,
        stitch_queue=pipeline.stitch_queue,
        user_pool=auth.user_pool,
        user_pool_client=auth.user_pool_client,
        environment=environment,
        git_sha="test-sha",
        openai_secret_name="dummy_secret",
    )
    return Template.from_stack(api)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_synthesizes(environment: str) -> None:
    template = _synth_api_stack(environment)
    # 1 API Lambda + 1 PreSignUp trigger Lambda (from the nested AuthStack
    # construct tree -- but AuthStack is a separate stack, so only the API
    # Lambda lands in *this* stack's template).
    #
    # PLANS/phase-6.md Q17: "no new Lambda" is a concurrency-budget promise
    # (2 synthesize + 1 extract + 2 stitch = 5 of an account-wide 10), so it
    # is enforced here rather than remembered. The reader adds *requests* to
    # this function, not functions.
    template.resource_count_is("AWS::Lambda::Function", 2)
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert any("/health" in key for key in route_keys)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_route_shape(environment: str) -> None:
    """PLANS/phase-6.md §16's addendum, as corrected after PR #7.

    9 routes, not the original 25: `ANY` on the four public paths, and
    enumerated methods on the authorized catch-all. The route reduction is a
    teardown mitigation -- two of the last three `destroy-pr` runs failed
    with HttpApiCognitoAuthorizer returning `InternalFailure` on delete,
    stranding four pr-N stacks each time; the failure is transient (both pr-4
    and pr-6 deleted cleanly on retry, no code change) with no observable
    service-side cause, so we provoke it less hard and retry it in
    destroy-pr.yml."""
    template = _synth_api_stack(environment)

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = sorted(r["Properties"]["RouteKey"] for r in routes.values())

    assert route_keys == [
        "ANY /docs",
        "ANY /health",
        "ANY /openapi.json",
        "ANY /redoc",
        "DELETE /{proxy+}",
        "GET /{proxy+}",
        "HEAD /{proxy+}",
        "POST /{proxy+}",
        "PUT /{proxy+}",
    ]
    template.resource_count_is("AWS::ApiGatewayV2::Route", 9)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_no_authorized_route_matches_options(environment: str) -> None:
    """The regression test for the CORS outage on PR #7.

    `ANY /{proxy+}` matched OPTIONS, and a matched route takes precedence
    over HttpApi's automatic cors_preflight response -- so every browser
    preflight hit the JWT authorizer and came back 401, and the browser then
    refused every cross-origin request. Measured against the deployed pr-7
    API before the fix:

        HTTP/2 401
        www-authenticate: Bearer
        {"message":"Unauthorized"}

    Nothing else catches this. Local compose talks to FastAPI directly, whose
    CORSMiddleware answers preflights, so API Gateway is never in the loop;
    only a real browser against a real deploy sees it. Hence an explicit
    assertion on the property that actually matters: no route carrying an
    authorizer may match an OPTIONS request."""
    template = _synth_api_stack(environment)

    for route in template.find_resources("AWS::ApiGatewayV2::Route").values():
        props = route["Properties"]
        if props["AuthorizationType"] == "NONE":
            continue  # public: a preflight reaching FastAPI is answered fine
        method = props["RouteKey"].split(" ", 1)[0]
        assert method not in ("ANY", "OPTIONS"), (
            f"{props['RouteKey']} is authorized and matches OPTIONS -- CORS "
            f"preflights will 401 and every cross-origin request will fail"
        )


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

    proxy_route = by_key["GET /{proxy+}"]
    assert proxy_route["AuthorizationType"] == "JWT"
    assert "AuthorizerId" in proxy_route

    # Asserted for EVERY public path, not just /health: after the ANY-route
    # collapse (PLANS/phase-6.md §16) there is exactly one route per path, so
    # getting one of them wrong either exposes the API or breaks it, with no
    # sibling route left to mask the mistake.
    for path in ("/health", "/docs", "/redoc", "/openapi.json"):
        public_route = by_key[f"ANY {path}"]
        assert public_route["AuthorizationType"] == "NONE", path
        assert "AuthorizerId" not in public_route, path


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


# --- phase 6 additions (PLANS/phase-6.md §13.3) -----------------------------


def _api_role_actions(template) -> list[str]:
    actions: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)
    return actions


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_has_queue_urls(environment: str) -> None:
    """POST /books/{id}/resynthesize needs both -- the fan-out for the chunks
    it rewinds, and the direct stitch publish for the zero-failed-chunk
    branch."""
    template = _synth_api_stack(environment)

    lambdas = template.find_resources("AWS::Lambda::Function")
    fn = next(res for name, res in lambdas.items() if name.startswith("ApiFunction"))
    env = fn["Properties"]["Environment"]["Variables"]

    assert "SYNTHESIZE_QUEUE_URL" in env
    assert "STITCH_QUEUE_URL" in env
    assert "EXTRACT_QUEUE_URL" in env


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_grants_send_to_all_three_queues(environment: str) -> None:
    """The **count**, not merely the presence: with a bare `in` check, losing
    one of the three grants would still pass. A missing grant surfaces only
    as a 500 from /resynthesize in a deployed environment."""
    template = _synth_api_stack(environment)

    actions = _api_role_actions(template)

    assert actions.count("sqs:SendMessage") == 3


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_still_reads_audio_and_marks(environment: str) -> None:
    """Regression guard on the grants the phase-6 read path depends on and
    which are granted only *incidentally*, by `grant_read_write`: the
    presigned URL's validity rests on s3:GetObject over audio_bucket, and
    GET /books/{id}/manifest reads marks_bucket directly."""
    template = _synth_api_stack(environment)

    actions = _api_role_actions(template)

    assert "s3:GetObject*" in actions
    # audio_bucket + marks_bucket + pdf_bucket.
    assert actions.count("s3:GetObject*") >= 3


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_function_shape(environment: str) -> None:
    """No ReservedConcurrentExecutions, so re-adding it fails a unit test
    instead of a six-minute deploy.

    This account's *total* Lambda concurrency limit is 10 and AWS rejects
    every possible value ("decreases account's UnreservedConcurrentExecution
    below its minimum value of [10]"), in prod exactly as in pr-N -- caught as
    a CREATE_FAILED on PR #5 (PLANS/phase-4.md §0's §6.3 correction). The
    synthesize and stitch functions already carry this assertion; phase 6 is
    what finally puts real request load on the API function, so it gets one
    too."""
    template = _synth_api_stack(environment)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {"ReservedConcurrentExecutions": Match.absent()},
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_chat_function(environment: str) -> None:
    from stacks.config import Config
    
    template = _synth_api_stack(environment)
    
    # 1. ChatFunction exists with specific memory, timeout, arch
    template.has_resource_properties("AWS::Lambda::Function", {
        "MemorySize": 256,
        "Timeout": 30,
        "Architectures": ["arm64"],
        "Environment": {
            "Variables": Match.object_like({
                Config.ENV_OPENAI_SECRET_NAME: "dummy_secret",
                Config.ENV_COGNITO_USER_POOL_ID: Match.any_value(),
            })
        }
    })

    # 2. Function URL config
    template.has_resource_properties("AWS::Lambda::Url", {
        "AuthType": "NONE",
        "InvokeMode": "RESPONSE_STREAM",
        "Cors": {
            "AllowOrigins": ["*"],
            "AllowMethods": ["POST"],
            "AllowHeaders": ["*"]
        }
    })

    # 3. Secrets manager grant
    template.has_resource_properties("AWS::IAM::Policy", {
        "PolicyDocument": {
            "Statement": Match.array_with([
                Match.object_like({
                    "Action": [
                        "secretsmanager:GetSecretValue",
                        "secretsmanager:DescribeSecret"
                    ],
                    "Effect": "Allow"
                })
            ])
        }
    })

@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_frontend_chat_wiring(environment: str) -> None:
    from stacks.config import Config
    
    app = cdk.App()
    stack = FrontendStack(
        app,
        f"TestFrontend-{environment}",
        environment=environment,
        api_base_url="api_url",
        chat_base_url="chat_url",
        cognito_client_id="c_id",
        cognito_region="c_region",
    )
    template = Template.from_stack(stack)
    
    template.has_resource_properties("AWS::Lambda::Function", {
        "Environment": {
            "Variables": Match.object_like({
                Config.ENV_CHAT_BASE_URL: "chat_url"
            })
        }
    })
