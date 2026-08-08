import os

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .config import Config, queue_name


class PipelineStack(cdk.Stack):
    """Extract/synthesize SQS queues + DLQs, the extract Lambda and its
    S3 -> SQS trigger (phase 3), and (phase 4) the synthesize Lambda
    attached to ``synthesize_queue`` -- fed by the extract Lambda, which is
    also a ``SynthesisQueue`` producer (``synthesize_queue.grant_send_
    messages(extract_fn)``).

    ``pdf_bucket_name``/``table`` are threaded in (rather than the actual
    ``StorageStack`` constructs) so this stack can **re-import** the pdf
    bucket by name -- see ``_add_extract_lambda`` for why that's load-bearing,
    not incidental. ``audio_bucket``/``marks_bucket`` are threaded in as real
    constructs (not re-imported by name): they need only IAM grants, no S3
    notification and no bucket policy, so passing the real construct creates
    a plain ``Pipeline -> Storage`` dependency that already exists -- no
    circular-dependency trap to route around (PLANS/phase-4.md §6.1).
    """

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        environment: str,
        pdf_bucket_name: str,
        audio_bucket: s3.IBucket,
        marks_bucket: s3.IBucket,
        table: dynamodb.Table,
        google_tts_secret_name: str = "",
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
            "Synthesize",
            queue_name("synthesize", environment),
            # 6x the synthesize Lambda's 300s timeout, same rule as
            # extract_queue.
            visibility_timeout=cdk.Duration.minutes(30),
            # 5, not 3: throttling from the ESM's MaximumConcurrency returns
            # the message to the queue and *does* bump ApproximateReceiveCount,
            # so a 3-attempt budget can be consumed by backpressure alone,
            # DLQ-ing perfectly good chunks on a large book. Kept in
            # lockstep with backend/src/config.py's
            # synthesize_max_receive_count (§8.3).
            max_receive_count=Config.SYNTHESIZE_MAX_RECEIVE_COUNT,
        )

        cdk.CfnOutput(self, "ExtractQueueUrl", value=self.extract_queue.queue_url)
        cdk.CfnOutput(self, "SynthesizeQueueUrl", value=self.synthesize_queue.queue_url)

        extract_fn = self._add_extract_lambda(
            pdf_bucket_name=pdf_bucket_name, table=table, git_sha=git_sha, environment=environment
        )
        self._add_synthesize_lambda(
            audio_bucket=audio_bucket,
            marks_bucket=marks_bucket,
            table=table,
            git_sha=git_sha,
            environment=environment,
            google_tts_secret_name=google_tts_secret_name,
        )

        # The fan-out producer grant -- the extract Lambda publishes to
        # synthesize_queue as the last step of ExtractBook.execute (PLANS/
        # phase-4.md §4). Easy to forget since it's the one grant that
        # points the "wrong" direction (extract -> synthesize queue, not
        # synthesize -> its own queue via the ESM).
        self.synthesize_queue.grant_send_messages(extract_fn)
        extract_fn.add_environment(Config.ENV_SYNTHESIZE_QUEUE_URL, self.synthesize_queue.queue_url)

    def _add_extract_lambda(
        self, *, pdf_bucket_name: str, table: dynamodb.Table, git_sha: str, environment: str
    ) -> lambda_.DockerImageFunction:
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
        # shared by two (now three) Lambda functions with different
        # entrypoints. A separate zip-based function would mean a second
        # dependency set and PyMuPDF's native MuPDF binary fighting the
        # Lambda zip size limit -- exactly what the container-image path
        # (ApiStack's own comment) already exists to avoid.
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
        return extract_fn

    def _add_synthesize_lambda(
        self,
        *,
        audio_bucket: s3.IBucket,
        marks_bucket: s3.IBucket,
        table: dynamodb.Table,
        git_sha: str,
        environment: str,
        google_tts_secret_name: str,
    ) -> None:
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

        # Third function, same image -- exactly the extract Lambda's
        # reasoning above. One ECR image, three Lambdas (ApiFunction,
        # ExtractFunction, SynthesizeFunction), three ImageConfig.Commands.
        synthesize_fn = lambda_.DockerImageFunction(
            self,
            "SynthesizeFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                backend_dir,
                cmd=["src.contexts.library.interface.synthesize_handler.handler"],
            ),
            # I/O-bound (websocket + HTTPS), not CPU-bound like extraction.
            # 1024 MB is chosen for Lambda's memory-proportional *network*
            # bandwidth, not for compute; the only CPU work is the MP3
            # frame scan.
            memory_size=1024,
            timeout=cdk.Duration.seconds(300),
            # Backstop only. The ESM's max_concurrency below is the real
            # throttle; this is deliberately HIGHER than it, because
            # CreateEventSourceMapping rejects a ScalingConfig.
            # MaximumConcurrency that exceeds the function's reserved
            # concurrency.
            reserved_concurrent_executions=10,
            environment={
                Config.ENV_ENVIRONMENT: environment,
                Config.ENV_GIT_SHA: git_sha,
                Config.ENV_TABLE_NAME: table.table_name,
                Config.ENV_AUDIO_BUCKET: audio_bucket.bucket_name,
                Config.ENV_MARKS_BUCKET: marks_bucket.bucket_name,
                Config.ENV_SYNTHESIZE_QUEUE_URL: self.synthesize_queue.queue_url,
                Config.ENV_EDGE_TTS_VOICE: Config.DEFAULT_EDGE_TTS_VOICE,
                Config.ENV_GOOGLE_TTS_VOICE: Config.DEFAULT_GOOGLE_TTS_VOICE,
                Config.ENV_GOOGLE_TTS_SECRET_NAME: google_tts_secret_name,
                Config.ENV_SYNTHESIZE_MAX_RECEIVE_COUNT: str(Config.SYNTHESIZE_MAX_RECEIVE_COUNT),
                Config.ENV_LOG_LEVEL: "INFO",
            },
        )

        # grant_put, not grant_read_write -- the synthesize Lambda never
        # reads or deletes audio/marks, only writes them.
        audio_bucket.grant_put(synthesize_fn)
        marks_bucket.grant_put(synthesize_fn)
        table.grant_read_write_data(synthesize_fn)

        if google_tts_secret_name:
            # Out of band (PLANS/phase-4.md §6.4): CDK never sees the secret
            # value, only its name. Optional -- if unset (every PR env by
            # default), the grant is skipped and get_speech_synthesizer()
            # falls back to a bare EdgeTtsSynthesizer with no Google fallback.
            secret = secretsmanager.Secret.from_secret_name_v2(
                self, "GoogleTtsSecret", google_tts_secret_name
            )
            secret.grant_read(synthesize_fn)  # also grants kms:Decrypt where needed

        synthesize_fn.add_event_source(
            lambda_event_sources.SqsEventSource(
                self.synthesize_queue,
                batch_size=1,
                # ScalingConfig.MaximumConcurrency; minimum allowed is 2.
                # Real reasoning: the free Edge Read Aloud endpoint is an
                # undocumented consumer service with no published rate
                # limit -- 5 concurrent websocket sessions from one Lambda
                # account is roughly "one person with five browser tabs"
                # and defensible; 50 would be abuse. Using max_concurrency
                # (not just reserved_concurrent_executions) is what makes
                # the SQS poller itself back off instead of the invocations
                # getting throttled and messages returning to the queue,
                # burning the DLQ budget on backpressure alone.
                max_concurrency=Config.SYNTHESIZE_MAX_CONCURRENCY,
            )
        )

        self.synthesize_function = synthesize_fn
        cdk.CfnOutput(self, "SynthesizeFunctionName", value=synthesize_fn.function_name)

    def _queue_with_dlq(
        self, logical_id: str, name: str, *, visibility_timeout: cdk.Duration, max_receive_count: int = 3
    ) -> sqs.Queue:
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
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=max_receive_count, queue=dlq),
        )
