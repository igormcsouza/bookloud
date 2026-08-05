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
                Config.ENV_LOG_LEVEL: "INFO",
            },
        )

        table.grant_read_write_data(fn)
        pdf_bucket.grant_read_write(fn)
        audio_bucket.grant_read_write(fn)
        marks_bucket.grant_read_write(fn)
        extract_queue.grant_send_messages(fn)

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

        # OPTIONS is deliberately excluded so preflight requests fall through
        # to API Gateway's built-in CORS auto-response (configured above)
        # instead of being matched by these routes -- and so CORS preflights
        # bypass the authorizer below (no OPTIONS route means no OPTIONS
        # authorization to configure).
        route_methods = [
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
        public_paths = ["/health", "/docs", "/redoc", "/openapi.json"]
        for path in public_paths:
            http_api.add_routes(
                path=path,
                methods=route_methods,
                integration=integration,
                authorizer=apigwv2.HttpNoneAuthorizer(),
            )

        http_api.add_routes(
            path="/{proxy+}",
            methods=route_methods,
            integration=integration,
            authorizer=authorizer,
        )

        self.http_api = http_api
        self.api_function = fn

        cdk.CfnOutput(self, "ApiUrl", value=http_api.url or "")
        cdk.CfnOutput(self, "ApiFunctionName", value=fn.function_name)
