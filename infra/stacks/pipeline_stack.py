import os

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .config import Config, queue_name


class PipelineStack(cdk.Stack):
    """Extract/synthesize SQS queues + DLQs, plus (phase 3) the extract
    Lambda and its S3 -> SQS trigger. The synthesize Lambda (phase 4) attaches
    to ``synthesize_queue`` later without a queue-shape change.

    ``pdf_bucket_name``/``table`` are threaded in (rather than the actual
    ``StorageStack`` constructs) so this stack can **re-import** the bucket by
    name -- see ``_add_extract_lambda`` for why that's load-bearing, not
    incidental.
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        pdf_bucket_name: str,
        table: dynamodb.Table,
        git_sha: str = "local",
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        self.extract_queue = self._queue_with_dlq(
            "Extract",
            queue_name("extract", environment),
            # 6x the extract Lambda's 120s timeout (AWS's own guidance) --
            # shorter than the function timeout would cause duplicate
            # concurrent invocations on the *same* message. The §8.2 atomic
            # EXTRACTING claim would catch that, but the queue should be
            # correct on its own.
            visibility_timeout=cdk.Duration.minutes(12),
        )
        self.synthesize_queue = self._queue_with_dlq(
            "Synthesize", queue_name("synthesize", environment), visibility_timeout=cdk.Duration.minutes(5)
        )

        cdk.CfnOutput(self, "ExtractQueueUrl", value=self.extract_queue.queue_url)
        cdk.CfnOutput(self, "SynthesizeQueueUrl", value=self.synthesize_queue.queue_url)

        self._add_extract_lambda(
            pdf_bucket_name=pdf_bucket_name, table=table, git_sha=git_sha, environment=environment
        )

    def _add_extract_lambda(
        self, *, pdf_bucket_name: str, table: dynamodb.Table, git_sha: str, environment: str
    ) -> None:
        # THE CIRCULAR-DEPENDENCY TRAP (PLANS/phase-3.md §6.1): `pdf_bucket`
        # lives in StorageStack, `extract_queue` here in PipelineStack.
        # Calling `storage.pdf_bucket.add_event_notification(...)` would put
        # the NotificationConfiguration in StorageStack (needs the queue ARN
        # -> Storage->Pipeline) and the queue's resource policy in
        # PipelineStack (needs the bucket ARN -> Pipeline->Storage) -- a hard
        # CloudFormation cycle. Re-importing the bucket *by name* here keeps
        # both the notification custom resource and the queue policy in this
        # one stack, leaving exactly one dependency direction:
        # Pipeline -> Storage.
        pdf_bucket = s3.Bucket.from_bucket_name(self, "PdfBucketRef", pdf_bucket_name)

        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

        # Second DockerImageFunction over the *same* backend/ image asset as
        # ApiStack's `fn` -- DockerImageAsset's hash is computed from the
        # source directory + build args, not `cmd` (which just becomes the
        # function's ImageConfig.Command), so this publishes one ECR image
        # shared by two Lambda functions with different entrypoints. A
        # separate zip-based function would mean a second dependency set and
        # PyMuPDF's native MuPDF binary fighting the Lambda zip size limit --
        # exactly what the container-image path (ApiStack's own comment)
        # already exists to avoid.
        extract_fn = lambda_.DockerImageFunction(
            self,
            "ExtractFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                backend_dir,
                cmd=["src.contexts.library.interface.extract_handler.handler"],
            ),
            # PyMuPDF text extraction is CPU-bound; more memory is
            # (proportionally) faster at roughly constant cost on Lambda.
            memory_size=1536,
            timeout=cdk.Duration.seconds(120),
            environment={
                Config.ENV_ENVIRONMENT: environment,
                Config.ENV_GIT_SHA: git_sha,
                Config.ENV_TABLE_NAME: table.table_name,
                Config.ENV_PDF_BUCKET: pdf_bucket.bucket_name,
                Config.ENV_LOG_LEVEL: "INFO",
            },
        )

        pdf_bucket.grant_read(extract_fn)
        table.grant_read_write_data(extract_fn)

        # batch_size=1 -- one pathological PDF can never poison a batch, and
        # it makes partial-batch-failure reporting unnecessary.
        extract_fn.add_event_source(
            lambda_event_sources.SqsEventSource(self.extract_queue, batch_size=1)
        )

        pdf_bucket.add_event_notification(
            s3.EventType.OBJECT_CREATED,
            s3n.SqsDestination(self.extract_queue),
            s3.NotificationKeyFilter(prefix=Config.SOURCE_PDF_PREFIX, suffix=".pdf"),
        )

        self.extract_function = extract_fn
        cdk.CfnOutput(self, "ExtractFunctionName", value=extract_fn.function_name)

    def _queue_with_dlq(self, logical_id: str, name: str, *, visibility_timeout: cdk.Duration) -> sqs.Queue:
        dlq = sqs.Queue(
            self,
            f"{logical_id}Dlq",
            queue_name=f"{name}-dlq",
            retention_period=cdk.Duration.days(4),
        )
        return sqs.Queue(
            self,
            logical_id,
            queue_name=name,
            visibility_timeout=visibility_timeout,
            retention_period=cdk.Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )
