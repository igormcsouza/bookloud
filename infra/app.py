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

# Api imports Storage + Pipeline. AuthStack is intentionally not referenced
# here in Phase 0 -- Phase 1 adds the JWT authorizer wiring.
api = ApiStack(
    app,
    stack_name("Api", environment),
    table=storage.table,
    pdf_bucket=storage.pdf_bucket,
    audio_bucket=storage.audio_bucket,
    marks_bucket=storage.marks_bucket,
    extract_queue=pipeline.extract_queue,
    environment=environment,
    git_sha=git_sha,
    env=env,
)

# Frontend imports Api.
FrontendStack(
    app,
    stack_name("Frontend", environment),
    api_base_url=api.http_api.url or "",
    environment=environment,
    env=env,
)

app.synth()
