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


# --- PipelineStack -----------------------------------------------------


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_synthesizes(environment: str) -> None:
    template = _synth(PipelineStack, environment)
    template.resource_count_is("AWS::SQS::Queue", 4)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_pipeline_stack_has_redrive_policies(environment: str) -> None:
    template = _synth(PipelineStack, environment)
    queues_with_redrive = template.find_resources(
        "AWS::SQS::Queue", {"Properties": {"RedrivePolicy": Match.any_value()}}
    )
    # extract + synthesize each have a redrive policy pointing at their DLQ;
    # the two DLQs themselves do not.
    assert len(queues_with_redrive) == 2


# --- FrontendStack -------------------------------------------------------


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_frontend_stack_synthesizes_without_a_build(environment: str) -> None:
    """No `.open-next` build exists in this checkout -> the placeholder Lambda
    code path is exercised, proving synth never hard-depends on a frontend
    build."""
    template = _synth(FrontendStack, environment, api_base_url="https://api.example.com")
    template.resource_count_is("AWS::CloudFront::Distribution", 1)


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_frontend_stack_has_two_cache_behaviours(environment: str) -> None:
    template = _synth(FrontendStack, environment, api_base_url="https://api.example.com")
    (distribution,) = template.find_resources("AWS::CloudFront::Distribution").values()
    config = distribution["Properties"]["DistributionConfig"]
    assert "DefaultCacheBehavior" in config
    assert len(config["CacheBehaviors"]) == 1


# --- Stack naming ----------------------------------------------------------


@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize(
    "stack_cls,component",
    [
        (StorageStack, "Storage"),
        (AuthStack, "Auth"),
        (PipelineStack, "Pipeline"),
    ],
)
def test_stack_naming_convention(stack_cls, component, environment: str) -> None:
    app = cdk.App()
    construct_id = f"Bookloud{component}-{environment}"
    stack = stack_cls(app, construct_id, environment=environment)
    assert stack.stack_name == construct_id


# --- ApiStack + full app wiring (requires Docker) ---------------------------


@pytest.mark.docker
@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_api_stack_synthesizes(environment: str) -> None:
    from stacks.api_stack import ApiStack

    app = cdk.App()
    storage = StorageStack(app, f"TestStorage-{environment}", environment=environment)
    pipeline = PipelineStack(app, f"TestPipeline-{environment}", environment=environment)
    api = ApiStack(
        app,
        f"TestApi-{environment}",
        table=storage.table,
        pdf_bucket=storage.pdf_bucket,
        audio_bucket=storage.audio_bucket,
        marks_bucket=storage.marks_bucket,
        extract_queue=pipeline.extract_queue,
        environment=environment,
        git_sha="test-sha",
    )
    template = Template.from_stack(api)
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::ApiGatewayV2::Api", 1)

    routes = template.find_resources("AWS::ApiGatewayV2::Route")
    route_keys = {r["Properties"]["RouteKey"] for r in routes.values()}
    assert any("/health" in key for key in route_keys)
