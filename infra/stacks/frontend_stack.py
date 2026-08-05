import os

import aws_cdk as cdk
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from constructs import Construct

from .config import Config, is_prod

# Served while the real OpenNext bundle hasn't been built yet, so `cdk synth`
# (and therefore infra/tests/test_synth.py and the CI synth check) keeps
# working with no frontend build.
_PLACEHOLDER = (
    "exports.handler = async () => ({"
    " statusCode: 200,"
    " headers: { 'content-type': 'text/html' },"
    " body: '<html><body><h1>Bookloud is deploying…</h1></body></html>' });"
)


class FrontendStack(cdk.Stack):
    """Next.js SSR on Lambda behind CloudFront. Private S3 assets bucket +
    a Lambda (Node runtime) from OpenNext's ``server-functions/default``
    output + Function URL + CloudFront distribution with two behaviours.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        api_base_url: str,
        cognito_client_id: str,
        cognito_region: str,
        environment: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prod = is_prod(environment)

        self.assets_bucket = s3.Bucket(
            self,
            "FrontendAssets",
            removal_policy=cdk.RemovalPolicy.RETAIN if prod else cdk.RemovalPolicy.DESTROY,
            auto_delete_objects=not prod,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
        )

        open_next_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "..", "frontend", ".open-next")
        )
        server_fn_dir = os.path.join(open_next_dir, "server-functions", "default")
        assets_dir = os.path.join(open_next_dir, "assets")

        # Phase-0 (and every phase before a real frontend build exists)
        # synthesises this stack with no OpenNext build present -> fall back
        # to the placeholder so synthesis doesn't fail. CI's deploy workflow
        # deletes cdk.out before the real frontend deploy, forcing a fresh
        # synthesis where the real build is present.
        if os.path.isdir(server_fn_dir):
            fn_code: lambda_.Code = lambda_.Code.from_asset(server_fn_dir)
        else:
            fn_code = lambda_.Code.from_inline(_PLACEHOLDER)

        fn = lambda_.Function(
            self,
            "FrontendFunction",
            runtime=lambda_.Runtime.NODEJS_24_X,
            handler="index.handler",
            code=fn_code,
            memory_size=1024,
            timeout=cdk.Duration.seconds(30),
            environment={
                Config.ENV_API_BASE_URL: api_base_url,
                # Server-only, deliberately NOT NEXT_PUBLIC_* -- read at
                # runtime by the Next.js BFF route handlers
                # (frontend/lib/cognito.ts), never baked into the client
                # bundle. No COGNITO_ENDPOINT: unset/empty in AWS makes
                # lib/cognito.ts derive the real Cognito regional endpoint.
                Config.ENV_COGNITO_CLIENT_ID: cognito_client_id,
                Config.ENV_COGNITO_REGION: cognito_region,
                # Read by the signup route handler to enforce Q3 (email
                # required only in prod) server-side.
                Config.ENV_ENVIRONMENT: environment,
                # OpenNext reads BUCKET_NAME to locate ISR cache in S3.
                "BUCKET_NAME": self.assets_bucket.bucket_name,
            },
        )

        self.assets_bucket.grant_read(fn)

        if os.path.isdir(assets_dir):
            s3deploy.BucketDeployment(
                self,
                "StaticAssets",
                sources=[s3deploy.Source.asset(assets_dir)],
                destination_bucket=self.assets_bucket,
            )

        self.function_url = fn.add_function_url(auth_type=lambda_.FunctionUrlAuthType.NONE)

        # CloudFront sits in front of both origins:
        #   /_next/static/*  ->  S3 (private bucket via OAC, long-lived cache)
        #   everything else  ->  Lambda Function URL (no cache, all methods)
        s3_origin = origins.S3BucketOrigin.with_origin_access_control(self.assets_bucket)
        lambda_origin = origins.FunctionUrlOrigin(self.function_url)

        # ALL_VIEWER_EXCEPT_HOST_HEADER (below) must strip Host before it
        # reaches the Function URL origin — Function URLs reject a forwarded
        # Host that doesn't match their own domain. That leaves the Lambda
        # with no way to know the public CloudFront domain, which the SSR
        # layer needs to build absolute URLs. This CloudFront Function hands
        # it over as a regular header instead, which the origin request
        # policy forwards untouched since it only excludes "Host" by name.
        forward_host_fn = cloudfront.Function(
            self,
            "ForwardHostHeader",
            code=cloudfront.FunctionCode.from_inline(
                "function handler(event) {\n"
                "  var request = event.request;\n"
                "  request.headers['x-forwarded-host'] = { value: request.headers.host.value };\n"
                "  return request;\n"
                "}"
            ),
            runtime=cloudfront.FunctionRuntime.JS_2_0,
        )

        distribution = cloudfront.Distribution(
            self,
            "Distribution",
            default_behavior=cloudfront.BehaviorOptions(
                origin=lambda_origin,
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                cache_policy=cloudfront.CachePolicy.CACHING_DISABLED,
                allowed_methods=cloudfront.AllowedMethods.ALLOW_ALL,
                origin_request_policy=cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
                function_associations=[
                    cloudfront.FunctionAssociation(
                        function=forward_host_fn,
                        event_type=cloudfront.FunctionEventType.VIEWER_REQUEST,
                    )
                ],
            ),
            additional_behaviors={
                "/_next/static/*": cloudfront.BehaviorOptions(
                    origin=s3_origin,
                    viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                    cache_policy=cloudfront.CachePolicy.CACHING_OPTIMIZED,
                ),
            },
        )

        cdk.CfnOutput(
            self, "FrontendUrl", value=f"https://{distribution.distribution_domain_name}"
        )
        cdk.CfnOutput(self, "AssetsBucketName", value=self.assets_bucket.bucket_name)
