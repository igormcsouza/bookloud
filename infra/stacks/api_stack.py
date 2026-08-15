import os

import aws_cdk as cdk
from aws_cdk import aws_apigatewayv2 as apigwv2
from aws_cdk import aws_apigatewayv2_authorizers as apigwv2_authorizers
from aws_cdk import aws_apigatewayv2_integrations as apigwv2_integrations
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .config import Config, http_api_name


class ApiStack(cdk.Stack):
    """Health-check Lambda + HTTP API, now with a Cognito JWT authorizer
    (Phase 1's "the Phase 0 deferral"): ``HttpUserPoolAuthorizer`` guards the
    ``/{proxy+}`` catch-all; the health/docs routes stay explicitly public via
    ``HttpNoneAuthorizer``. The backend Lambda itself never validates tokens
    — see ``backend/src/auth/dependencies.py``.

    Phase 6 adds two things and nothing else: ``sqs:SendMessage`` on the
    synthesize and stitch queues (``POST /books/{id}/resynthesize`` publishes
    to both), and the ``ANY``-route collapse described below. The reader's
    four new GET routes need **no** stack change at all — they sit under the
    existing ``/{proxy+}``, and presigning ``audio_bucket``/reading
    ``marks_bucket`` are already covered by the ``grant_read_write`` calls
    below.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        table: dynamodb.Table,
        pdf_bucket: s3.Bucket,
        audio_bucket: s3.Bucket,
        marks_bucket: s3.Bucket,
        extract_queue: sqs.Queue,
        # PLANS/phase-6.md §11 -- both new this phase, both for
        # POST /books/{id}/resynthesize: the fan-out for the chunks it rewinds,
        # and the direct stitch publish for the zero-failed-chunk
        # (STITCH_FAILED) branch, where no chunk will ever increment again.
        synthesize_queue: sqs.Queue,
        stitch_queue: sqs.Queue,
        user_pool: cognito.IUserPool,
        user_pool_client: cognito.IUserPoolClient,
        environment: str,
        git_sha: str,
        openai_secret_name: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        backend_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "backend")
        )

        # Container image (not a zip): phases 3-4 pull in PyMuPDF and
        # edge-tts, which blow past the Lambda zip layer size limits.
        # Establishing the container path now avoids a migration later.
        fn = lambda_.DockerImageFunction(
            self,
            "ApiFunction",
            # Explicit target: `backend/Dockerfile` gained a `lambda-stream`
            # stage after `lambda` for phase 7's ChatFunction (FROM lambda AS
            # lambda-stream), which silently changed which stage `docker
            # build` picks when no --target is given -- it's the LAST stage
            # in the file, not necessarily `lambda`. Every function that
            # relied on that default (this one, and pipeline_stack.py's
            # three) was building the wrong image (uvicorn/chat_app, no RIC)
            # until this was made explicit -- caught by a real deploy
            # returning 500 on every route including /health.
            code=lambda_.DockerImageCode.from_image_asset(backend_dir, target="lambda"),
            memory_size=512,
            timeout=cdk.Duration.seconds(30),
            environment={
                Config.ENV_ENVIRONMENT: environment,
                Config.ENV_GIT_SHA: git_sha,
                Config.ENV_TABLE_NAME: table.table_name,
                Config.ENV_PDF_BUCKET: pdf_bucket.bucket_name,
                Config.ENV_AUDIO_BUCKET: audio_bucket.bucket_name,
                Config.ENV_MARKS_BUCKET: marks_bucket.bucket_name,
                Config.ENV_EXTRACT_QUEUE_URL: extract_queue.queue_url,
                Config.ENV_SYNTHESIZE_QUEUE_URL: synthesize_queue.queue_url,
                Config.ENV_STITCH_QUEUE_URL: stitch_queue.queue_url,
                Config.ENV_LOG_LEVEL: "INFO",
            },
            # NO reserved_concurrent_executions, here or anywhere. This
            # account's *total* Lambda concurrency limit is 10 and AWS rejects
            # every possible value ("decreases account's
            # UnreservedConcurrentExecution below its minimum value of [10]"),
            # in prod exactly as in pr-N -- caught as a CREATE_FAILED on PR #5
            # (PLANS/phase-4.md §0's §6.3 correction). The assertion in
            # infra/tests/test_synth.py means re-adding it fails a unit test
            # instead of a six-minute deploy. Stated on the API function too
            # because phase 6 is what finally puts real request load on it.
        )

        table.grant_read_write_data(fn)
        pdf_bucket.grant_read_write(fn)
        # grant_read_write is what makes the phase-6 presigned GET valid at
        # all (s3:GetObject on audio_bucket) and what lets the manifest/marks
        # routes read marks_bucket. Granted incidentally since phase 4, so
        # test_synth.py keeps an explicit regression guard on it.
        audio_bucket.grant_read_write(fn)
        marks_bucket.grant_read_write(fn)
        extract_queue.grant_send_messages(fn)
        synthesize_queue.grant_send_messages(fn)
        stitch_queue.grant_send_messages(fn)

        http_api = apigwv2.HttpApi(
            self,
            "HttpApi",
            api_name=http_api_name(environment),
            cors_preflight=apigwv2.CorsPreflightOptions(
                allow_origins=["*"],
                allow_headers=["Authorization", "Content-Type"],
                allow_methods=[
                    apigwv2.CorsHttpMethod.GET,
                    apigwv2.CorsHttpMethod.POST,
                    apigwv2.CorsHttpMethod.PUT,
                    apigwv2.CorsHttpMethod.DELETE,
                    apigwv2.CorsHttpMethod.OPTIONS,
                ],
            ),
        )

        integration = apigwv2_integrations.HttpLambdaIntegration("Api", fn)

        # ONE `ANY` route per path, not five enumerated methods
        # (PLANS/phase-6.md §16's addendum). This used to be
        # 5 paths x 5 methods = 25 AWS::ApiGatewayV2::Route resources; it is
        # now 5.
        #
        # Why: two of the last three `destroy-pr` runs failed identically,
        # with HttpApiCognitoAuthorizer returning `HandlerErrorCode:
        # InternalFailure` on delete, stranding four pr-N stacks each time.
        # The pr-6 post-mortem established that CloudFormation ordered the
        # deletion correctly (all 25 routes reached DELETE_COMPLETE before
        # the authorizer was touched) and that the API had zero routes and
        # zero integrations left when the authorizer failed -- so it is
        # neither an ordering bug nor a still-in-use reference, and
        # `InternalFailure` carries no service-side reason. It is transient:
        # the identical delete succeeded on retry for both pr-4 and pr-6, with
        # no code change either time. Collapsing to `ANY` cuts the route
        # churn 5x, which reduces how hard the teardown provokes the failure;
        # `.github/workflows/destroy-pr.yml`'s bounded DELETE_FAILED retry is
        # the other half of the mitigation.
        #
        # `ANY` matches every method INCLUDING OPTIONS -- and that is why the
        # authorized catch-all below does NOT use it.
        #
        # An earlier revision of this phase collapsed `/{proxy+}` to `ANY`
        # too, on the assumption that HttpApi's cors_preflight (configured
        # above) makes API Gateway answer OPTIONS *before* route matching.
        # THAT ASSUMPTION IS FALSE. A matched route takes precedence over the
        # automatic CORS response, so every preflight went to the JWT
        # authorizer and came back 401, and the browser then refused every
        # cross-origin request. Measured against the deployed pr-7 API:
        #
        #   $ curl -i -X OPTIONS .../books -H 'Origin: https://…cloudfront.net' \
        #       -H 'Access-Control-Request-Method: GET'
        #   HTTP/2 401
        #   www-authenticate: Bearer
        #   {"message":"Unauthorized"}
        #
        # Nothing caught it before `deploy-pr`: local compose talks to FastAPI
        # directly (its CORSMiddleware answers preflights), so API Gateway is
        # never in the loop, and no unit test exercises a real browser.
        #
        # So the authorized path keeps its enumerated methods, deliberately
        # WITHOUT OPTIONS, which is what lets a preflight fall through to the
        # built-in auto-response and bypass the authorizer. Do not "simplify"
        # this to ANY.
        #
        # The public paths still use ANY: they carry no authorizer, so an
        # OPTIONS that matches them reaches the Lambda and FastAPI's
        # CORSMiddleware answers it correctly. Route count is 4 + 5 = 9 rather
        # than the original 25, so most of the teardown mitigation survives
        # (see the note above and destroy-pr.yml's retry for the other half).
        any_method = [apigwv2.HttpMethod.ANY]
        authorized_methods = [
            apigwv2.HttpMethod.GET,
            apigwv2.HttpMethod.HEAD,
            apigwv2.HttpMethod.POST,
            apigwv2.HttpMethod.PUT,
            apigwv2.HttpMethod.DELETE,
        ]

        authorizer = apigwv2_authorizers.HttpUserPoolAuthorizer(
            "CognitoAuthorizer",
            user_pool,
            user_pool_clients=[user_pool_client],
        )

        # Explicit public routes (HttpNoneAuthorizer is required, not
        # decorative: once any authorizer exists on the API, being explicit
        # here is what keeps these paths reachable without a token), plus a
        # catch-all requiring a valid Cognito JWT for everything else. No
        # default_authorizer on the HttpApi -- each route states its own.
        # The split is asserted per-route in infra/tests/test_synth.py:
        # getting it wrong either exposes the API or breaks it.
        public_paths = ["/health", "/docs", "/redoc", "/openapi.json"]
        for path in public_paths:
            http_api.add_routes(
                path=path,
                methods=any_method,
                integration=integration,
                authorizer=apigwv2.HttpNoneAuthorizer(),
            )

        http_api.add_routes(
            path="/{proxy+}",
            methods=authorized_methods,
            integration=integration,
            authorizer=authorizer,
        )

        self.http_api = http_api
        self.api_function = fn

        # ApiFunction needs OPENAI_SECRET_NAME/CHAT_DAILY_LIMIT too -- it's
        # what GET /books/{id}/chat uses to compute chat.enabled/dailyLimit
        # (PLANS/phase-7.md §9.3) -- but deliberately gets NO secret grant
        # (see below): it can test the name for emptiness, never read the
        # value.
        fn.add_environment(Config.ENV_OPENAI_SECRET_NAME, openai_secret_name)
        fn.add_environment(Config.ENV_CHAT_DAILY_LIMIT, str(Config.CHAT_DAILY_LIMIT))

        cdk.CfnOutput(self, "ApiUrl", value=http_api.url or "")
        cdk.CfnOutput(self, "ApiFunctionName", value=fn.function_name)

        chat_env = {
            Config.ENV_ENVIRONMENT: environment,
            Config.ENV_GIT_SHA: git_sha,
            Config.ENV_TABLE_NAME: table.table_name,
            Config.ENV_PDF_BUCKET: pdf_bucket.bucket_name,
            Config.ENV_AUDIO_BUCKET: audio_bucket.bucket_name,
            Config.ENV_MARKS_BUCKET: marks_bucket.bucket_name,
            Config.ENV_COGNITO_USER_POOL_ID: user_pool.user_pool_id,
            Config.ENV_COGNITO_CLIENT_ID: user_pool_client.user_pool_client_id,
            Config.ENV_COGNITO_REGION: cdk.Stack.of(self).region,
            Config.ENV_OPENAI_SECRET_NAME: openai_secret_name,  # "" unless -c
            Config.ENV_OPENAI_MODEL: Config.DEFAULT_OPENAI_MODEL,
            Config.ENV_OPENAI_MAX_OUTPUT_TOKENS: str(Config.OPENAI_MAX_OUTPUT_TOKENS),
            Config.ENV_CHAT_DAILY_LIMIT: str(Config.CHAT_DAILY_LIMIT),
            Config.ENV_LOG_LEVEL: "INFO",
        }

        self.chat_fn = lambda_.DockerImageFunction(
            self, "ChatFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                backend_dir, target="lambda-stream"
            ),
            # X86_64, same as every other function in this stack (no
            # `architecture=` override -- that's the default). ARM_64 here
            # sets `--platform linux/arm64` on the DockerImageAsset build,
            # which needs QEMU/binfmt cross-arch emulation this account's CI
            # runners don't have: `RUN uv export ...` in the shared `lambda`
            # stage fails with "exec format error" on every build, because
            # the runner can pull an arm64 `ghcr.io/astral-sh/uv` binary but
            # cannot execute it. Reintroducing ARM_64 needs a
            # docker/setup-qemu-action step in every workflow that builds
            # this image first.
            memory_size=512,        # I/O-bound: one DynamoDB Query pair + one HTTPS stream
            timeout=cdk.Duration.seconds(120),
            environment=chat_env,
            # NO reserved_concurrent_executions -- same account-quota wall as
            # every other function here (PLANS/phase-4.md §0's §6.3
            # correction). infra/tests/test_synth.py asserts Match.absent()
            # so re-adding it fails a unit test instead of a six-minute
            # deploy.
        )
        table.grant_read_write_data(self.chat_fn)   # Query CHUNK#/CHAT#, UpdateItem quota, PutItem turns
        # No S3 grant, no SQS grant -- asserted in test_synth.py, because the
        # streaming function is the one with a public URL and its blast
        # radius should be exactly "this user's books".

        if openai_secret_name:
            from aws_cdk import aws_secretsmanager
            secret = aws_secretsmanager.Secret.from_secret_name_v2(self, "OpenAiSecret", openai_secret_name)
            secret.grant_read(self.chat_fn)
            # Deliberately NOT granted on `fn` (ApiFunction) -- it only needs
            # the *name* to test for emptiness, never read access to the
            # value (PLANS/phase-7.md §9.3, §13.3
            # test_api_function_cannot_read_the_secret).

        self.chat_url = self.chat_fn.add_function_url(
            auth_type=lambda_.FunctionUrlAuthType.NONE,
            invoke_mode=lambda_.InvokeMode.RESPONSE_STREAM,
            cors=lambda_.FunctionUrlCorsOptions(
                allowed_origins=["*"],
                allowed_methods=[lambda_.HttpMethod.POST],
                allowed_headers=["authorization", "content-type"],
                max_age=cdk.Duration.hours(1),
            ),
        )

        cdk.CfnOutput(self, "ChatUrl", value=self.chat_url.url)
