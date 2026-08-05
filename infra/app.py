#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.config import Config, stack_name
from stacks.frontend_stack import FrontendStack
from stacks.pipeline_stack import PipelineStack
from stacks.storage_stack import StorageStack

app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

environment: str = app.node.try_get_context("environment") or Config.DEFAULT_ENVIRONMENT
# Threaded into the API Lambda's env so local/smoke_test.py can assert the
# deployed code matches the commit under test.
git_sha: str = app.node.try_get_context("git_sha") or "local"

# Independent stacks first.
storage = StorageStack(app, stack_name("Storage", environment), environment=environment, env=env)
auth = AuthStack(app, stack_name("Auth", environment), environment=environment, env=env)
pipeline = PipelineStack(
    app, stack_name("Pipeline", environment), environment=environment, env=env
)

# Api imports Storage + Pipeline + Auth (the Cognito JWT authorizer).
api = ApiStack(
    app,
    stack_name("Api", environment),
    table=storage.table,
    pdf_bucket=storage.pdf_bucket,
    audio_bucket=storage.audio_bucket,
    marks_bucket=storage.marks_bucket,
    extract_queue=pipeline.extract_queue,
    user_pool=auth.user_pool,
    user_pool_client=auth.user_pool_client,
    environment=environment,
    git_sha=git_sha,
    env=env,
)

# Frontend imports Api + Auth (Cognito config for the SSR Lambda's BFF route
# handlers). Dependency graph: Storage, Auth, Pipeline -> Api -> Frontend,
# with Auth -> Frontend as well. Teardown order in destroy-pr.yml
# (Frontend -> Api -> Pipeline -> Auth -> Storage) is already correct for
# this and needs no change.
FrontendStack(
    app,
    stack_name("Frontend", environment),
    api_base_url=api.http_api.url or "",
    cognito_client_id=auth.user_pool_client.user_pool_client_id,
    cognito_region=auth.region,
    environment=environment,
    env=env,
)

app.synth()
