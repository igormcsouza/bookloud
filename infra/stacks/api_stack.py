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
            code=lambda_.DockerImageCode.from_image_asset(backend_dir),
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
        # `ANY` matches every method INCLUDING OPTIONS, which the enumerated
        # list deliberately excluded so CORS preflights would fall through to
        # API Gateway's built-in auto-response and bypass the authorizer.
        # That exclusion is preserved a different way and MUST stay preserved:
        # HttpApi's cors_preflight (configured above) makes API Gateway answer
        # OPTIONS itself *before* route matching, so a preflight never reaches
        # these routes and never reaches the JWT authorizer. Were that ever
        # removed, `ANY /{proxy+}` would send preflights to the authorizer,
        # which 401s them for lack of an Authorization header -- and every
        # cross-origin request from the browser would break.
        any_method = [apigwv2.HttpMethod.ANY]

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
            methods=any_method,
            integration=integration,
            authorizer=authorizer,
        )

        self.http_api = http_api
        self.api_function = fn

        cdk.CfnOutput(self, "ApiUrl", value=http_api.url or "")
        cdk.CfnOutput(self, "ApiFunctionName", value=fn.function_name)
