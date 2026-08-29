#!/usr/bin/env python3
import os

import aws_cdk as cdk

from stacks.api_stack import ApiStack
from stacks.auth_stack import AuthStack
from stacks.config import Config, stack_name
from stacks.pipeline_stack import PipelineStack
from stacks.storage_stack import StorageStack

app = cdk.App()

# Region is deliberately NOT branched on `environment` here -- every stack
# just follows whatever CDK_DEFAULT_REGION the CLI resolves at synth time
# from the active AWS credentials/profile. The split between prod
# (sa-east-1) and staging (us-east-1) lives entirely in
# .github/workflows/deploy-prod.yml's per-job configure-aws-credentials +
# `cdk bootstrap` region, via separate secrets (AWS_REGION_PROD vs
# AWS_REGION). Locally, CDK_DEFAULT_REGION comes from whatever the
# developer's AWS CLI/profile defaults to.
#
# Every downstream stack that needs the deployed region at runtime reads it
# off `cdk.Stack.of(self).region`, which CDK resolves from this same `env`
# -- so nothing else needs to change when the region changes per
# environment; see infra/tests/test_synth.py's test_stack_region_follows_env
# for a synth-level check of this.
env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION"),
)

environment: str = app.node.try_get_context("environment") or Config.DEFAULT_ENVIRONMENT
# Threaded into the API Lambda's env so local/smoke_test.py can assert the
# deployed code matches the commit under test.
git_sha: str = app.node.try_get_context("git_sha") or "local"

# Storage and Auth are independent of each other; Pipeline now imports
# Storage (it re-imports pdf_bucket by name and takes the table/audio/marks
# buckets directly -- see pipeline_stack.py's module docstring for why that
# avoids a circular CloudFormation dependency with the S3 -> SQS
# notification). Dependency chain: Storage -> Pipeline -> Api, with
# Auth -> Api alongside it.
storage = StorageStack(app, stack_name("Storage", environment), environment=environment, env=env)
auth = AuthStack(app, stack_name("Auth", environment), environment=environment, env=env)
pipeline = PipelineStack(
    app,
    stack_name("Pipeline", environment),
    environment=environment,
    pdf_bucket_name=storage.pdf_bucket.bucket_name,
    audio_bucket=storage.audio_bucket,
    marks_bucket=storage.marks_bucket,
    table=storage.table,
    # "" (unset) EVERYWHERE by default, prod included -- this is what OQ-A's
    # "ship the Google fallback dormant" actually requires. Defaulting prod to
    # Config.GOOGLE_TTS_SECRET_NAME is not dormant: get_speech_synthesizer()
    # calls get_secret() eagerly while *building* the synthesizer, before
    # edge-tts is ever attempted, so a prod stack pointing at a secret that
    # does not exist yet fails every chunk with ResourceNotFoundException --
    # including the chunks edge-tts (which needs no credentials at all) would
    # have synthesized fine.
    # Turning the fallback on is therefore a deliberate two-step, in this
    # order: create the secret out of band (PLANS/phase-4.md §6.4 --
    # `aws secretsmanager create-secret --name bookloud/google-tts-api-key`),
    # then deploy with -c google_tts_secret_name=bookloud/google-tts-api-key.
    # Note the override alone still never turns on real calls outside prod --
    # get_speech_synthesizer()'s ENVIRONMENT == "prod" gate (§0) is
    # unconditional and checked first.
    google_tts_secret_name=app.node.try_get_context("google_tts_secret_name") or "",
    git_sha=git_sha,
    env=env,
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
    # PLANS/phase-6.md §11: POST /books/{id}/resynthesize publishes to both.
    # The Api -> Pipeline edge already exists (extract_queue), so this adds no
    # new dependency direction and no cycle.
    synthesize_queue=pipeline.synthesize_queue,
    stitch_queue=pipeline.stitch_queue,
    user_pool=auth.user_pool,
    user_pool_client=auth.user_pool_client,
    environment=environment,
    git_sha=git_sha,
    openai_secret_name=app.node.try_get_context("openai_secret_name") or "",
    env=env,
)

app.synth()
