"""``aws_cdk.assertions`` coverage over every stack, for `environment="pr-99"`,
`environment="staging"` and `environment="prod"`. Runs with no AWS
credentials.

`"staging"` was added in phase 8 (`IMPLEMENTATION_PLAN.md`): `deploy-prod.yml`
now deploys an ephemeral `Bookloud*-staging` stack set as its e2e gate before
touching `-prod`. It is a non-prod environment name exactly like `pr-99` (only
`environment == "prod"` flips `is_prod()`), so it is expected to behave
identically to `pr-99` everywhere below -- this just proves that generalizing
beyond `pr-<number>` names didn't quietly break anything that assumed the
`pr-` prefix.

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
from stacks.pipeline_stack import PipelineStack
from stacks.storage_stack import StorageStack

ENVIRONMENTS = ["pr-99", "staging", "prod"]


def _synth(stack_cls, environment: str, **kwargs) -> Template:
    app = cdk.App()
    stack = stack_cls(
        app,
        f"Test{stack_cls.__name__}-{environment}",
        environment=environment,
        **kwargs,
    )
    return Template.from_stack(stack)


# --- Region split (issue #8) ---------------------------------------------
#
# infra/app.py doesn't branch region on `environment` in code -- every stack
# just takes whatever `cdk.Environment(region=...)` it's constructed with,
# which in the real pipelines comes from CDK_DEFAULT_REGION as resolved by
# the CDK CLI from whichever AWS credentials configure-aws-credentials set
# up (AWS_REGION_PROD/sa-east-1 in deploy-prod.yml, AWS_REGION/us-east-1 in
# deploy-pr.yml). These tests exercise that mechanism directly: a stack
# constructed with an explicit `env=` for each region must synthesize with
# that region on `Stack.region`, and any resource that mirrors the stack's
# own region into a runtime env var (wired from `cdk.Stack.of(self).region`
# in api_stack.py) must follow it too.
@pytest.mark.parametrize(
    ("environment", "region"),
    [("prod", "sa-east-1"), ("pr-99", "us-east-1")],
)
def test_stack_region_follows_env(environment: str, region: str) -> None:
    app = cdk.App()
    stack = StorageStack(
        app,
        f"TestStorageRegion-{environment}",
        environment=environment,
        env=cdk.Environment(account="111111111111", region=region),
    )
    assert stack.region == region


@pytest.mark.parametrize(
    ("environment", "region"),
    [("prod", "sa-east-1"), ("pr-99", "us-east-1")],
)
def test_auth_stack_region_follows_env(environment: str, region: str) -> None:
    app = cdk.App()
    stack = AuthStack(
        app,
        f"TestAuthRegion-{environment}",
        environment=environment,
        env=cdk.Environment(account="111111111111", region=region),
    )
    assert stack.region == region


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
    # phase-8 DLQ-sweeper Lambda + the (auto-created, inline-Python,
    # no-Docker) BucketNotificationsHandler singleton that an *imported*
    # bucket's add_event_notification synthesizes.
    template.resource_count_is("AWS::Lambda::Function", 5)


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
    # extract + synthesize + stitch queues, plus the phase-8 sweeper's two
    # (extract DLQ, synthesize DLQ).
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 5)
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
    (`synthesize_queue.grant_send_messages(extract_fn)`), on phase 5's
    fan-in twin (`stitch_queue.grant_send_messages(synthesize_fn)`), and on
    phase 8's third producer grant
    (`stitch_queue.grant_send_messages(sweeper_fn)`) -- all three point the
    "wrong" direction (a Lambda granted send access to a queue it isn't
    itself subscribed to)."""
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
    # THREE producer grants, asserted by count so losing any one fails:
    # extract -> synthesize_queue, synthesize -> stitch_queue, and the phase-8
    # DLQ sweeper -> stitch_queue.
    assert send_message_statements == 3
    # The stitch Lambda is the first function that READS audio_bucket -- the
    # synthesize Lambda deliberately only has grant_put.
    assert "s3:GetObject*" in actions or "s3:GetObject" in actions
    # grant_put covers the multipart upload's Abort permission.
    assert any(a.startswith("s3:Abort") for a in actions)


# --- Phase 8: DLQ sweeper Lambda ----------------------------------------


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_dlq_sweeper_function_shape(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "ImageConfig": {"Command": ["src.contexts.library.interface.dlq_sweep_handler.handler"]},
            "Timeout": 30,
            "MemorySize": 256,
        },
    )


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_dlq_sweeper_env_vars(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    functions = template.find_resources(
        "AWS::Lambda::Function",
        {
            "Properties": {
                "ImageConfig": {"Command": ["src.contexts.library.interface.dlq_sweep_handler.handler"]}
            }
        },
    )
    (props,) = [r["Properties"] for r in functions.values()]
    variables = props["Environment"]["Variables"]
    assert "TABLE_NAME" in variables
    assert "STITCH_QUEUE_URL" in variables
    # The sweeper never calls a TTS engine, extracts a PDF, or touches
    # audio/marks -- none of that configuration belongs on it.
    assert "GOOGLE_TTS_SECRET_NAME" not in variables
    assert "AUDIO_BUCKET" not in variables
    assert "PDF_BUCKET" not in variables


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_dlq_sweeper_event_sources_are_extract_and_synthesize_dlqs_only(
    environment: str,
) -> None:
    """The sweeper is fed by the extract and synthesize DLQs, never the
    stitch DLQ (interface/dlq_sweep_handler.py's module docstring)."""
    template = _synth_pipeline_stack(environment)
    mappings = template.find_resources("AWS::Lambda::EventSourceMapping")

    dlqs = template.find_resources(
        "AWS::SQS::Queue", {"Properties": {"QueueName": Match.string_like_regexp(r"^bookloud-.*-dlq$")}}
    )
    dlq_logical_ids_by_name = {
        props["Properties"]["QueueName"]: logical_id for logical_id, props in dlqs.items()
    }
    extract_dlq_id = dlq_logical_ids_by_name[f"bookloud-{environment}-extract-dlq"]
    synthesize_dlq_id = dlq_logical_ids_by_name[f"bookloud-{environment}-synthesize-dlq"]
    stitch_dlq_id = dlq_logical_ids_by_name[f"bookloud-{environment}-stitch-dlq"]

    referenced_dlq_ids = set()
    for mapping in mappings.values():
        source_arn = mapping["Properties"]["EventSourceArn"]
        if isinstance(source_arn, dict) and "Fn::GetAtt" in source_arn:
            referenced_dlq_ids.add(source_arn["Fn::GetAtt"][0])

    assert extract_dlq_id in referenced_dlq_ids
    assert synthesize_dlq_id in referenced_dlq_ids
    assert stitch_dlq_id not in referenced_dlq_ids


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_dlq_sweeper_grants(environment: str) -> None:
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

    assert "dynamodb:UpdateItem" in actions
    assert "dynamodb:GetItem" in actions
    assert "sqs:SendMessage" in actions


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_extract_and_synthesize_dlqs_have_a_visibility_timeout_for_the_sweeper(
    environment: str,
) -> None:
    """6x the sweeper Lambda's 30s timeout -- the same rule every
    consumer/queue pair in this stack follows, now applied to the two DLQs
    that gained a consumer in phase 8."""
    template = _synth_pipeline_stack(environment)
    for name in ("extract", "synthesize"):
        dlqs = template.find_resources(
            "AWS::SQS::Queue",
            {"Properties": {"QueueName": Match.string_like_regexp(f"^bookloud-.*-{name}-dlq$")}},
        )
        (props,) = [r["Properties"] for r in dlqs.values()]
        assert props["VisibilityTimeout"] == 180

    # stitch-dlq has no consumer, so it keeps the SQS default (30s) rather
    # than being sized off a Lambda that never reads it.
    stitch_dlqs = template.find_resources(
        "AWS::SQS::Queue", {"Properties": {"QueueName": Match.string_like_regexp("^bookloud-.*-stitch-dlq$")}}
    )
    (stitch_props,) = [r["Properties"] for r in stitch_dlqs.values()]
    assert stitch_props.get("VisibilityTimeout", 30) == 30


# --- Phase 8: CloudWatch alarms ------------------------------------------


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_dlq_depth_alarms_exist_for_all_three_queues(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    alarms = template.find_resources("AWS::CloudWatch::Alarm")
    alarm_names = {a["Properties"]["AlarmName"] for a in alarms.values()}

    for kind in ("extract", "synthesize", "stitch"):
        assert f"bookloud-{environment}-{kind}-dlq-depth" in alarm_names


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_dlq_depth_alarm_shape(environment: str) -> None:
    template = _synth_pipeline_stack(environment)
    alarms = template.find_resources(
        "AWS::CloudWatch::Alarm", {"Properties": {"AlarmName": f"bookloud-{environment}-extract-dlq-depth"}}
    )
    (props,) = [a["Properties"] for a in alarms.values()]
    assert props["MetricName"] == "ApproximateNumberOfMessagesVisible"
    assert props["Namespace"] == "AWS/SQS"
    assert props["Threshold"] == 1
    assert props["EvaluationPeriods"] == 1
    assert props["ComparisonOperator"] == "GreaterThanOrEqualToThreshold"
    assert props["TreatMissingData"] == "notBreaching"


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_tts_fallback_metric_filter_and_alarm(environment: str) -> None:
    """PLANS/phase-4.md OQ-E: a metric filter over the synthesize Lambda's
    log group matching the fallback-synthesizer's literal warning token,
    feeding an alarm."""
    template = _synth_pipeline_stack(environment)

    filters = template.find_resources("AWS::Logs::MetricFilter")
    assert len(filters) == 1
    (filter_props,) = [f["Properties"] for f in filters.values()]
    assert "TTS_FALLBACK_TRIGGERED" in filter_props["FilterPattern"]
    assert filter_props["MetricTransformations"][0]["MetricNamespace"] == "Bookloud"

    alarms = template.find_resources(
        "AWS::CloudWatch::Alarm", {"Properties": {"AlarmName": f"bookloud-{environment}-tts-fallback-triggered"}}
    )
    assert len(alarms) == 1
    (alarm_props,) = [a["Properties"] for a in alarms.values()]
    assert alarm_props["Threshold"] == 1
    assert alarm_props["EvaluationPeriods"] == 1
    assert alarm_props["TreatMissingData"] == "notBreaching"


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


def _synth_api_stack(environment: str, *, openai_secret_name: str = "dummy_secret"):
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
        openai_secret_name=openai_secret_name,
    )
    return Template.from_stack(api)


def _function_role_logical_id(template, name_prefix: str) -> str:
    lambdas = template.find_resources("AWS::Lambda::Function")
    fn = next(res for name, res in lambdas.items() if name.startswith(name_prefix))
    return fn["Properties"]["Role"]["Fn::GetAtt"][0]


def _actions_for_role(template, role_logical_id: str) -> list[str]:
    actions: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        roles = policy["Properties"].get("Roles", [])
        if not any(isinstance(r, dict) and r.get("Ref") == role_logical_id for r in roles):
            continue
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)
    return actions


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
    """PLANS/phase-7.md §9.3: 512 MB / 120 s -- I/O-bound (one DynamoDB
    Query pair + one HTTPS stream to OpenAI), not the API function's 30 s."""
    from stacks.config import Config

    template = _synth_api_stack(environment)

    # 1. ChatFunction exists with specific memory, timeout, arch
    template.has_resource_properties("AWS::Lambda::Function", {
        "MemorySize": 512,
        "Timeout": 120,
        "Environment": {
            "Variables": Match.object_like({
                Config.ENV_OPENAI_SECRET_NAME: "dummy_secret",
                Config.ENV_COGNITO_USER_POOL_ID: Match.any_value(),
            })
        }
    })

    # 2. Function URL config -- CORS AllowHeaders scoped to what the browser
    # actually sends (Authorization: Bearer + Content-Type), not "*".
    template.has_resource_properties("AWS::Lambda::Url", {
        "AuthType": "NONE",
        "InvokeMode": "RESPONSE_STREAM",
        "Cors": {
            "AllowOrigins": ["*"],
            "AllowMethods": ["POST"],
            "AllowHeaders": ["authorization", "content-type"],
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


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_chat_function_env(environment: str) -> None:
    """PLANS/phase-7.md §13.3: ChatFunction carries everything it needs to
    verify a Cognito token itself and to build/gate an OpenAiChatModel."""
    from stacks.config import Config

    template = _synth_api_stack(environment)

    template.has_resource_properties("AWS::Lambda::Function", {
        "Environment": {
            "Variables": Match.object_like({
                Config.ENV_COGNITO_USER_POOL_ID: Match.any_value(),
                Config.ENV_COGNITO_CLIENT_ID: Match.any_value(),
                Config.ENV_COGNITO_REGION: Match.any_value(),
                Config.ENV_OPENAI_SECRET_NAME: Match.any_value(),
                Config.ENV_OPENAI_MODEL: "gpt-4.1-mini",
                Config.ENV_CHAT_DAILY_LIMIT: "50",
            })
        },
    })


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_lambda_count(environment: str) -> None:
    """A count assertion, not a presence one: Q4's concurrency-budget promise
    (PLANS/phase-7.md §9.4) is exactly 2 Lambdas in ApiStack (Api + Chat), not
    "at least 2"."""
    template = _synth_api_stack(environment)

    assert len(template.find_resources("AWS::Lambda::Function")) == 2


@pytest.mark.docker
@pytest.mark.parametrize("environment", ["dev", "pr-1", "prod"])
def test_openai_secret_name_defaults_empty_in_every_environment(environment: str) -> None:
    """The constraint-2 regression test, and the direct analogue of the
    phase-4 OQ-A correction: OPENAI_SECRET_NAME is "" by default -- prod
    included -- and with no name, zero GetSecretValue statements exist
    anywhere in the stack (PLANS/phase-7.md §5.2, §13.3)."""
    from stacks.config import Config

    template = _synth_api_stack(environment, openai_secret_name="")

    template.has_resource_properties("AWS::Lambda::Function", {
        "Environment": {
            "Variables": Match.object_like({Config.ENV_OPENAI_SECRET_NAME: ""}),
        },
    })

    actions: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                actions.extend(action)
            elif isinstance(action, str):
                actions.append(action)
    assert "secretsmanager:GetSecretValue" not in actions


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_openai_secret_grant_appears_with_context_flag(environment: str) -> None:
    """With the context flag, the env var is set AND exactly one
    GetSecretValue grant appears, on ChatFunction only."""
    template = _synth_api_stack(environment, openai_secret_name="bookloud/openai-api-key")

    all_actions: list[str] = []
    for policy in template.find_resources("AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            action = statement.get("Action")
            if isinstance(action, list):
                all_actions.extend(action)
            elif isinstance(action, str):
                all_actions.append(action)
    assert all_actions.count("secretsmanager:GetSecretValue") == 1

    chat_role = _function_role_logical_id(template, "ChatFunction")
    assert "secretsmanager:GetSecretValue" in _actions_for_role(template, chat_role)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_function_cannot_read_the_secret(environment: str) -> None:
    """Even with the context flag, ApiFunction's role has no
    GetSecretValue statement -- it only tests OPENAI_SECRET_NAME for
    emptiness, never reads the value (PLANS/phase-7.md §9.3)."""
    template = _synth_api_stack(environment, openai_secret_name="bookloud/openai-api-key")

    api_role = _function_role_logical_id(template, "ApiFunction")
    assert "secretsmanager:GetSecretValue" not in _actions_for_role(template, api_role)


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_chat_function_has_no_s3_or_sqs_grants(environment: str) -> None:
    """The function with a public URL touches the table and nothing else
    (PLANS/phase-7.md §13.3)."""
    template = _synth_api_stack(environment)

    chat_role = _function_role_logical_id(template, "ChatFunction")
    actions = _actions_for_role(template, chat_role)

    assert not any(a.startswith("s3:") for a in actions if isinstance(a, str))
    assert not any(a.startswith("sqs:") for a in actions if isinstance(a, str))


@pytest.mark.docker
def test_no_stack_template_contains_a_key_shaped_string() -> None:
    """PLANS/phase-7.md §5.5 rule 7: every synthesized ApiStack template,
    with and without the context flag, is searched for `sk-`-prefixed
    values. The check that would have caught someone "temporarily"
    hardcoding a key."""
    import json

    for environment in ("dev", "pr-1", "prod"):
        for secret_name in ("", "bookloud/openai-api-key"):
            template = _synth_api_stack(environment, openai_secret_name=secret_name)
            text = json.dumps(template.to_json())
            assert "sk-" not in text
