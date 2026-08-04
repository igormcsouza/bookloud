import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_s3 as s3
from constructs import Construct

from .config import is_prod, table_name


class StorageStack(cdk.Stack):
    """DynamoDB single table + the three S3 buckets from
    IMPLEMENTATION_PLAN.md's data model. Anything not specified there (GSIs,
    streams, lifecycle rules) is deferred to phase 2+.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        prod = is_prod(environment)
        removal_policy = cdk.RemovalPolicy.RETAIN if prod else cdk.RemovalPolicy.DESTROY

        # Single table, PK/SK per IMPLEMENTATION_PLAN.md's data model:
        #   USER#<id>  / BOOK#<bookId>  -> book metadata
        #   BOOK#<id>  / CHUNK#<n>      -> chunk text/audio/marks
        #   BOOK#<id>  / CHAT#<msgId>   -> chat turns
        self.table = dynamodb.Table(
            self,
            "Table",
            table_name=table_name(environment),
            partition_key=dynamodb.Attribute(name="PK", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="SK", type=dynamodb.AttributeType.STRING),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=prod
            ),
            removal_policy=removal_policy,
        )

        bucket_defaults = dict(
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            versioned=False,
            removal_policy=removal_policy,
            auto_delete_objects=not prod,
        )

        # CORS so phase 3's presigned browser upload works without a stack
        # change: PUT/POST/GET from any origin (the same permissive CORS
        # posture as ApiStack's HTTP API).
        self.pdf_bucket = s3.Bucket(
            self,
            "PdfBucket",
            cors=[
                s3.CorsRule(
                    allowed_methods=[s3.HttpMethods.PUT, s3.HttpMethods.POST, s3.HttpMethods.GET],
                    allowed_origins=["*"],
                    allowed_headers=["*"],
                )
            ],
            **bucket_defaults,
        )
        self.audio_bucket = s3.Bucket(self, "AudioBucket", **bucket_defaults)
        self.marks_bucket = s3.Bucket(self, "MarksBucket", **bucket_defaults)

        cdk.CfnOutput(self, "TableName", value=self.table.table_name)
        cdk.CfnOutput(self, "PdfBucketName", value=self.pdf_bucket.bucket_name)
        cdk.CfnOutput(self, "AudioBucketName", value=self.audio_bucket.bucket_name)
        cdk.CfnOutput(self, "MarksBucketName", value=self.marks_bucket.bucket_name)
