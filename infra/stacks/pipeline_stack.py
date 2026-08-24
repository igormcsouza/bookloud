import os

import aws_cdk as cdk
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as events_targets
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_lambda_event_sources as lambda_event_sources
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_notifications as s3n
from aws_cdk import aws_secretsmanager as secretsmanager
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .config import Config, queue_name


class PipelineStack(cdk.Stack):
    """Extract/synthesize/stitch SQS queues + DLQs, the extract Lambda and
    its S3 -> SQS trigger (phase 3), the synthesize Lambda that polls
    ``synthesize_queue`` -- fed by the extract Lambda, which is also a
    ``SynthesisQueue`` producer (``synthesize_queue.grant_send_messages(
    extract_fn)``) -- and (phase 5) the stitch Lambda that polls
    ``stitch_queue``, fed by the synthesize Lambda's fan-in edge
    (``stitch_queue.grant_send_messages(synthesize_fn)``). Each of the three
    polls its own queue on an EventBridge Rule schedule rather than via an
    SQS event source mapping -- see ``_add_scheduled_pollers`` for why.

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

        # Populated by _queue_with_dlq, keyed by logical id ("Extract",
        # "Synthesize", "Stitch") -- see that method's comment.
        self.dlqs: dict[str, sqs.Queue] = {}

        self.extract_queue = self._queue_with_dlq(
            "Extract",
            queue_name("extract", environment),
            # 6x the extract Lambda's 120s timeout (AWS's own guidance) --
            # shorter than the function timeout would cause duplicate
            # concurrent invocations on the *same* message. The §8.2 atomic
            # EXTRACTING claim would catch that, but the queue should be
            # correct on its own.
            visibility_timeout=cdk.Duration.minutes(12),
            # 6x the DLQ sweeper Lambda's 30s timeout (PLANS/phase-3.md
            # OQ-4) -- the same rule as every consumer/queue pair in this
            # stack, applied to the DLQ itself now that it has a consumer.
            dlq_visibility_timeout=cdk.Duration.minutes(3),
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
            # Same DLQ-sweeper reasoning as extract_queue's DLQ above.
            dlq_visibility_timeout=cdk.Duration.minutes(3),
        )

        self.stitch_queue = self._queue_with_dlq(
            "Stitch",
            queue_name("stitch", environment),
            # 6x the stitch Lambda's 900s timeout, the same rule extract_queue
            # and synthesize_queue already follow -- a shorter visibility
            # timeout would hand the same book to a second invocation
            # mid-concatenation. The §6.4 conditional claim would catch that,
            # but the queue should be right on its own.
            visibility_timeout=cdk.Duration.minutes(90),
            max_receive_count=Config.STITCH_MAX_RECEIVE_COUNT,
            # Default 30s -- deliberately NOT consumed by the DLQ sweeper
            # (see interface/dlq_sweep_handler.py's module docstring), so
            # there is no consumer timeout to size this off of.
        )

        cdk.CfnOutput(self, "ExtractQueueUrl", value=self.extract_queue.queue_url)
        cdk.CfnOutput(self, "SynthesizeQueueUrl", value=self.synthesize_queue.queue_url)
        cdk.CfnOutput(self, "StitchQueueUrl", value=self.stitch_queue.queue_url)

        extract_fn = self._add_extract_lambda(
            pdf_bucket_name=pdf_bucket_name, table=table, git_sha=git_sha, environment=environment
        )
        synthesize_fn = self._add_synthesize_lambda(
            audio_bucket=audio_bucket,
            marks_bucket=marks_bucket,
            table=table,
            git_sha=git_sha,
            environment=environment,
            google_tts_secret_name=google_tts_secret_name,
        )
        self._add_stitch_lambda(
            audio_bucket=audio_bucket,
            marks_bucket=marks_bucket,
            table=table,
            git_sha=git_sha,
            environment=environment,
        )
        self._add_dlq_sweeper_lambda(table=table, git_sha=git_sha, environment=environment)
        self._add_dlq_depth_alarms(environment=environment)
        self._add_tts_fallback_alarm(synthesize_fn=synthesize_fn, environment=environment)
        self._add_scheduled_pollers(
            extract_fn=extract_fn, synthesize_fn=synthesize_fn, stitch_fn=self.stitch_function
        )

        # The fan-out producer grant -- the extract Lambda publishes to
        # synthesize_queue as the last step of ExtractBook.execute (PLANS/
        # phase-4.md §4). Easy to forget since it's the one grant that
        # points the "wrong" direction (extract -> synthesize queue, not
        # synthesize -> its own queue via the ESM).
        self.synthesize_queue.grant_send_messages(extract_fn)
        extract_fn.add_environment(Config.ENV_SYNTHESIZE_QUEUE_URL, self.synthesize_queue.queue_url)

        # Phase 5's equivalent, and this phase's easy-to-forget grant: the
        # fan-IN producer. The synthesize Lambda publishes one stitch message
        # when its increment_chunks_done is the one that observes
        # chunksDone == chunksTotal (PLANS/phase-5.md §4.2).
        self.stitch_queue.grant_send_messages(synthesize_fn)
        synthesize_fn.add_environment(Config.ENV_STITCH_QUEUE_URL, self.stitch_queue.queue_url)

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
                target="lambda",  # explicit: see api_stack.py's ApiFunction comment
                cmd=["src.contexts.library.interface.extract_handler.scheduled_handler"],
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
                Config.ENV_EXTRACT_QUEUE_URL: self.extract_queue.queue_url,
                Config.ENV_LOG_LEVEL: "INFO",
            },
        )

        pdf_bucket.grant_read(extract_fn)
        table.grant_read_write_data(extract_fn)

        # No SqsEventSource here (unlike phase-3's original wiring) --
        # _add_scheduled_pollers attaches an EventBridge Rule instead, so
        # this Lambda polls itself on a schedule rather than an ESM poller
        # idling 24/7 (the SQS free-tier cost trap). grant_consume_messages
        # is the ESM's IAM half without the polling half: ReceiveMessage/
        # DeleteMessage/GetQueueAttributes on the function's role, nothing
        # more.
        self.extract_queue.grant_consume_messages(extract_fn)

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
    ) -> lambda_.DockerImageFunction:
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

        # Third function, same image -- exactly the extract Lambda's
        # reasoning above. One ECR image, three Lambdas (ApiFunction,
        # ExtractFunction, SynthesizeFunction), three ImageConfig.Commands.
        synthesize_fn = lambda_.DockerImageFunction(
            self,
            "SynthesizeFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                backend_dir,
                target="lambda",  # explicit: see api_stack.py's ApiFunction comment
                cmd=["src.contexts.library.interface.synthesize_handler.scheduled_handler"],
            ),
            # I/O-bound (websocket + HTTPS), not CPU-bound like extraction.
            # 1024 MB is chosen for Lambda's memory-proportional *network*
            # bandwidth, not for compute; the only CPU work is the MP3
            # frame scan.
            memory_size=1024,
            timeout=cdk.Duration.seconds(300),
            # NO reserved_concurrent_executions here, deliberately -- do not
            # "restore" it. This account's total Lambda concurrency limit is
            # 10, and AWS refuses any reservation that drops *unreserved*
            # concurrency below its floor of 10. So every possible value is
            # rejected with "Specified ReservedConcurrentExecutions ...
            # decreases account's UnreservedConcurrentExecution below its
            # minimum value of [10]", in prod exactly as in pr-N.
            # Nothing is lost: reserved concurrency was only ever a backstop
            # here (see PLANS/phase-4.md §6.3), and the real throttle on
            # concurrent TTS calls is now structural, not configured -- this
            # function has no SqsEventSource (_add_scheduled_pollers attaches
            # an EventBridge Rule instead), and its own schedule interval is
            # always longer than its timeout (Config.
            # SYNTHESIZE_POLL_INTERVAL_MINUTES's comment), so at most one
            # invocation is ever draining this queue at a time. Raising the
            # account quota is still the prerequisite for adding reserved
            # concurrency back, if that's ever wanted for its own sake.
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

        # No SqsEventSource here -- _add_scheduled_pollers attaches an
        # EventBridge Rule instead, so this Lambda polls itself on a
        # schedule rather than an ESM poller idling 24/7 on the free Edge
        # Read Aloud endpoint's queue (the SQS free-tier cost trap).
        # grant_consume_messages is the ESM's IAM half without the polling
        # half: ReceiveMessage/DeleteMessage/GetQueueAttributes on the
        # function's role, nothing more. The old ScalingConfig.
        # MaximumConcurrency=2 throttle on concurrent TTS calls is now moot:
        # with no ESM there is only ever at most one invocation of this
        # function draining the queue (see the timeout comment above).
        self.synthesize_queue.grant_consume_messages(synthesize_fn)

        self.synthesize_function = synthesize_fn
        cdk.CfnOutput(self, "SynthesizeFunctionName", value=synthesize_fn.function_name)
        return synthesize_fn

    def _add_stitch_lambda(
        self,
        *,
        audio_bucket: s3.IBucket,
        marks_bucket: s3.IBucket,
        table: dynamodb.Table,
        git_sha: str,
        environment: str,
    ) -> lambda_.DockerImageFunction:
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

        # Fourth function, same image -- DockerImageAsset's hash comes from
        # the source dir + build args + platform, NOT from `cmd`. One ECR
        # image, four Lambdas, four ImageConfig.Commands.
        stitch_fn = lambda_.DockerImageFunction(
            self,
            "StitchFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                backend_dir,
                target="lambda",  # explicit: see api_stack.py's ApiFunction comment
                cmd=["src.contexts.library.interface.stitch_handler.scheduled_handler"],
            ),
            # 1536 MB is NOT sized for the whole book: the multipart writer
            # (PLANS/phase-5.md §7.4) keeps resident bytes at O(one 5 MiB
            # part + one ~720 KB segment). The memory buys CPU for the frame
            # scan (~1.3M frames on a 300-page book) and network bandwidth
            # for ~330 GetObjects.
            memory_size=1536,
            # 900s is the Lambda maximum. Budget for a 330-chunk book: ~330
            # GETs at ~100ms plus ~48 UploadParts plus the frame scan --
            # comfortably inside, but there is no larger value available if
            # it ever isn't, which is why StitchBook logs a WARNING past
            # 300s of wall clock (OQ-5).
            timeout=cdk.Duration.seconds(900),
            # NO reserved_concurrent_executions here, deliberately -- do not
            # "restore" it. Identical account-quota wall as SynthesizeFunction
            # above: this account's total Lambda concurrency limit is 10 and
            # AWS refuses any reservation that drops unreserved concurrency
            # below its floor of 10, so every possible value is rejected, in
            # prod exactly as in pr-N. No SqsEventSource either (see below) --
            # with no ESM there is only ever at most one invocation of this
            # function draining the queue, so "one book per invocation" (the
            # old batch_size=1 concern) is structural now, not configured.
            environment={
                Config.ENV_ENVIRONMENT: environment,
                Config.ENV_GIT_SHA: git_sha,
                Config.ENV_TABLE_NAME: table.table_name,
                Config.ENV_AUDIO_BUCKET: audio_bucket.bucket_name,
                Config.ENV_MARKS_BUCKET: marks_bucket.bucket_name,
                Config.ENV_STITCH_QUEUE_URL: self.stitch_queue.queue_url,
                Config.ENV_STITCH_MAX_RECEIVE_COUNT: str(Config.STITCH_MAX_RECEIVE_COUNT),
                Config.ENV_LOG_LEVEL: "INFO",
            },
        )

        # This is the first function that READS audio_bucket -- the
        # synthesize Lambda deliberately only has grant_put. grant_put maps to
        # s3:PutObject* plus s3:Abort*, which together cover
        # CreateMultipartUpload/UploadPart/CompleteMultipartUpload/
        # AbortMultipartUpload, so no hand-written policy document is needed.
        audio_bucket.grant_read(stitch_fn)
        audio_bucket.grant_put(stitch_fn)
        marks_bucket.grant_put(stitch_fn)
        # GetItem/Query (chunks) + UpdateItem (the claim and the terminal
        # transition).
        table.grant_read_write_data(stitch_fn)

        # No SqsEventSource here -- _add_scheduled_pollers attaches an
        # EventBridge Rule instead, so this Lambda polls itself on a
        # schedule rather than an ESM poller idling 24/7 (the SQS free-tier
        # cost trap). grant_consume_messages is the ESM's IAM half without
        # the polling half: ReceiveMessage/DeleteMessage/GetQueueAttributes
        # on the function's role, nothing more.
        self.stitch_queue.grant_consume_messages(stitch_fn)

        self.stitch_function = stitch_fn
        cdk.CfnOutput(self, "StitchFunctionName", value=stitch_fn.function_name)
        return stitch_fn

    def _add_dlq_sweeper_lambda(
        self, *, table: dynamodb.Table, git_sha: str, environment: str
    ) -> lambda_.DockerImageFunction:
        """Phase 8 (``IMPLEMENTATION_PLAN.md``; deferred from PLANS/phase-3.md
        OQ-4, extended by PLANS/phase-5.md §4.3/OQ-4): fed by the extract and
        synthesize DLQs (never the stitch DLQ -- see
        ``interface/dlq_sweep_handler.py``'s module docstring for why).
        """
        backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))

        # Fifth function, same image -- DockerImageAsset's hash comes from
        # the source dir + build args + platform, NOT from `cmd`. One ECR
        # image, five Lambdas, five ImageConfig.Commands.
        sweeper_fn = lambda_.DockerImageFunction(
            self,
            "DlqSweeperFunction",
            code=lambda_.DockerImageCode.from_image_asset(
                backend_dir,
                target="lambda",  # explicit: see api_stack.py's ApiFunction comment
                cmd=["src.contexts.library.interface.dlq_sweep_handler.handler"],
            ),
            # Neither CPU- nor memory-bound: one GetItem/UpdateItem round
            # trip (plus, rarely, one SendMessage) per DLQ message. The
            # Lambda default floor is enough.
            memory_size=256,
            # Comfortably above the p99 for a handful of DynamoDB calls;
            # short enough that the 3-minute DLQ visibility timeout above
            # (6x this) stays a small number.
            timeout=cdk.Duration.seconds(30),
            environment={
                Config.ENV_ENVIRONMENT: environment,
                Config.ENV_GIT_SHA: git_sha,
                Config.ENV_TABLE_NAME: table.table_name,
                Config.ENV_STITCH_QUEUE_URL: self.stitch_queue.queue_url,
                Config.ENV_LOG_LEVEL: "INFO",
            },
        )

        # GetItem/Query (book + chunk) + UpdateItem (the FAILED/chunk-FAILED
        # writes and the counter increment) -- the same grant every other
        # pipeline Lambda in this stack gets.
        table.grant_read_write_data(sweeper_fn)
        # The sweeper's own STITCH_REQUEUED re-publish
        # (application/sweeping.py) -- the third producer grant onto this
        # queue, alongside the synthesize Lambda's own fan-in publish below.
        self.stitch_queue.grant_send_messages(sweeper_fn)

        # batch_size=1 on both -- one poison DLQ message must never block
        # another book's/chunk's recovery, and neither of these paths is a
        # rate-limited external call, so no ScalingConfig/max_concurrency is
        # needed (unlike the extract/synthesize/stitch Lambdas, all of which
        # throttle against a real external constraint).
        sweeper_fn.add_event_source(
            lambda_event_sources.SqsEventSource(self.dlqs["Extract"], batch_size=1)
        )
        sweeper_fn.add_event_source(
            lambda_event_sources.SqsEventSource(self.dlqs["Synthesize"], batch_size=1)
        )

        self.dlq_sweeper_function = sweeper_fn
        cdk.CfnOutput(self, "DlqSweeperFunctionName", value=sweeper_fn.function_name)
        return sweeper_fn

    def _add_dlq_depth_alarms(self, *, environment: str) -> None:
        """CloudWatch alarm on DLQ depth for all three pipeline queues
        (``IMPLEMENTATION_PLAN.md`` phase 8; deferred from PLANS/phase-3.md
        OQ-4, extended to synthesize/stitch by PLANS/phase-5.md's mention of
        the same phase-8 item). A steady-state DLQ is empty -- any message
        visible for a full evaluation period is itself the signal, extract/
        synthesize now additionally trigger the sweeper above, and stitch has
        no consumer at all (an alarm is the only notice for that one).

        ``treat_missing_data=NOT_BREACHING``: SQS only emits
        ApproximateNumberOfMessagesVisible datapoints when the queue *has*
        activity, so an idle DLQ (the overwhelmingly common case) would
        otherwise report NO_DATA -- alarm actions on INSUFFICIENT_DATA are
        not what "the DLQ has something in it" means.
        """
        for logical_id in ("Extract", "Synthesize", "Stitch"):
            dlq = self.dlqs[logical_id]
            cloudwatch.Alarm(
                self,
                f"{logical_id}DlqDepthAlarm",
                alarm_name=f"bookloud-{environment}-{logical_id.lower()}-dlq-depth",
                alarm_description=(
                    f"One or more messages stranded in the {logical_id.lower()} DLQ -- "
                    "see PLANS/phase-3.md OQ-4 / PLANS/phase-5.md §4.3 for the recovery story."
                ),
                metric=dlq.metric_approximate_number_of_messages_visible(period=cdk.Duration.minutes(5)),
                threshold=1,
                evaluation_periods=1,
                comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            )

    def _add_tts_fallback_alarm(
        self, *, synthesize_fn: lambda_.DockerImageFunction, environment: str
    ) -> None:
        """Metric filter + alarm on the synthesizer fallback warning
        (``IMPLEMENTATION_PLAN.md`` phase 8; deferred from PLANS/phase-4.md
        OQ-E): Google TTS silently becoming the primary engine should be
        noticed, not discovered on a bill.

        ``"TTS_FALLBACK_TRIGGERED"`` is a literal-string contract with
        ``infrastructure/fallback_synthesizer.py``'s warning log -- see that
        module's docstring. Matched with ``FilterPattern.any_term`` (a plain
        substring match over unstructured Lambda text logs, not JSON), so
        engine-name/exception-text changes in the rest of the log line can
        never break the filter.
        """
        # Created explicitly (not `synthesize_fn.log_group`, and not
        # `LogGroup.from_log_group_name` either) at the Lambda's conventional
        # `/aws/lambda/<function-name>` name. `synthesize_fn.log_group`
        # provisions a `LogRetention` custom-resource Lambda the first time
        # it's touched -- a sixth Lambda + role in this stack for a property
        # we only need to point a metric filter at. Importing *by name* was
        # tried first and fails on a genuinely fresh stack: the log group
        # doesn't exist until the Lambda's first invocation, and
        # `CreateMetricFilter` against a not-yet-existent log group is a
        # hard deploy-time failure (`ResourceNotFoundException`), not a
        # synth-time one -- `cdk synth`/infra tests never invoke the Lambda,
        # so this only surfaces on a real first deploy. Declaring our own
        # `LogGroup` construct at that same name sidesteps both problems: the
        # Lambda service writes into whatever log group already exists under
        # its conventional name, so this one just needs to exist first.
        log_group = logs.LogGroup(
            self,
            "SynthesizeLogGroup",
            log_group_name=f"/aws/lambda/{synthesize_fn.function_name}",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=cdk.RemovalPolicy.DESTROY,
        )
        metric_filter = logs.MetricFilter(
            self,
            "TtsFallbackMetricFilter",
            log_group=log_group,
            filter_pattern=logs.FilterPattern.any_term("TTS_FALLBACK_TRIGGERED"),
            metric_namespace="Bookloud",
            metric_name=f"TtsFallbackTriggered-{environment}",
            metric_value="1",
            default_value=0,
        )
        cloudwatch.Alarm(
            self,
            "TtsFallbackAlarm",
            alarm_name=f"bookloud-{environment}-tts-fallback-triggered",
            alarm_description=(
                "edge-tts failed and Google TTS covered for it at least once -- "
                "PLANS/phase-4.md OQ-E's free-tier-burn risk."
            ),
            metric=metric_filter.metric(
                statistic="sum",
                period=cdk.Duration.hours(1),
            ),
            threshold=1,
            evaluation_periods=1,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )

    def _add_scheduled_pollers(
        self,
        *,
        extract_fn: lambda_.DockerImageFunction,
        synthesize_fn: lambda_.DockerImageFunction,
        stitch_fn: lambda_.DockerImageFunction,
    ) -> None:
        """Replaces the extract/synthesize/stitch SqsEventSources with an
        EventBridge Rule apiece (the AWS free-tier usage alert this exists
        because of -- SQS at 864k/1M requests for August, almost entirely
        from idle infrastructure, not book uploads).

        The root cause the first attempt at this (commit that added
        ``receive_message_wait_time=cdk.Duration.seconds(20)`` on every
        queue in ``_queue_with_dlq``) missed: that attribute only controls
        long polling for consumers that call ``ReceiveMessage`` directly.
        Lambda's own SQS event-source-mapping poller always long-polls
        internally regardless of the queue's own setting, and keeps a
        minimum number of poller threads running against the queue whether
        or not it has ever had a message -- an ESM is simply never idle,
        which is exactly what a mostly-empty personal-scale pipeline is
        almost all of the time.

        The fix: no ESM at all. Each function's ``cmd`` now points at a
        ``scheduled_handler`` (see ``extract_handler.py`` etc.) that drains
        its own queue with ``poll_once`` -- the same receive/process/
        delete-on-success loop the local docker-compose workers already run
        against LocalStack -- until either the queue is empty or the
        invocation is close to its own timeout. An EventBridge Rule invokes
        that handler on a fixed schedule instead of an ESM invoking it
        continuously, so cost only accrues once per tick instead of once
        per ~10s of wall-clock time.

        Every function's target payload is the default EventBridge event
        (nothing app-specific): ``scheduled_handler`` ignores ``event``
        entirely, so there's nothing to construct here.
        """
        events.Rule(
            self,
            "ExtractPollSchedule",
            schedule=events.Schedule.rate(
                cdk.Duration.minutes(Config.EXTRACT_POLL_INTERVAL_MINUTES)
            ),
            targets=[events_targets.LambdaFunction(extract_fn)],
        )
        events.Rule(
            self,
            "SynthesizePollSchedule",
            schedule=events.Schedule.rate(
                cdk.Duration.minutes(Config.SYNTHESIZE_POLL_INTERVAL_MINUTES)
            ),
            targets=[events_targets.LambdaFunction(synthesize_fn)],
        )
        events.Rule(
            self,
            "StitchPollSchedule",
            schedule=events.Schedule.rate(
                cdk.Duration.minutes(Config.STITCH_POLL_INTERVAL_MINUTES)
            ),
            targets=[events_targets.LambdaFunction(stitch_fn)],
        )

    def _queue_with_dlq(
        self,
        logical_id: str,
        name: str,
        *,
        visibility_timeout: cdk.Duration,
        max_receive_count: int = 3,
        dlq_visibility_timeout: cdk.Duration = cdk.Duration.seconds(30),
    ) -> sqs.Queue:
        dlq = sqs.Queue(
            self,
            f"{logical_id}Dlq",
            queue_name=f"{name}-dlq",
            # Default (30s, SQS's own default) everywhere except the
            # extract/synthesize DLQs, which are also consumed by the phase-8
            # sweeper Lambda (PLANS/phase-3.md OQ-4, PLANS/phase-5.md §4.3) --
            # those pass a longer value sized off that Lambda's own timeout,
            # the same "6x the consumer's timeout" rule every other queue in
            # this stack follows.
            visibility_timeout=dlq_visibility_timeout,
            retention_period=cdk.Duration.days(4),
            # Long polling (SQS's 20s max). Without this every queue defaults
            # to ReceiveMessageWaitTimeSeconds=0 (short polling), and the
            # Lambda ESM's background poller then calls ReceiveMessage in a
            # tight loop even while the queue sits empty 24/7 -- this is what
            # burns the SQS free tier's 1M requests/month on idle
            # infrastructure, not on actual book traffic. Long polling makes
            # an idle poller block for up to 20s per call instead, cutting
            # empty-queue request volume by roughly two orders of magnitude
            # with zero effect on delivery latency for a real message.
            receive_message_wait_time=cdk.Duration.seconds(20),
        )
        queue = sqs.Queue(
            self,
            logical_id,
            queue_name=name,
            visibility_timeout=visibility_timeout,
            retention_period=cdk.Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=max_receive_count, queue=dlq),
            # See the DLQ's identical comment above -- same fix, same reason.
            receive_message_wait_time=cdk.Duration.seconds(20),
        )
        # Stashed by logical id (e.g. self.dlqs["Extract"]) so
        # __init__/_add_dlq_sweeper_lambda/_add_dlq_alarms can reach every
        # DLQ without re-deriving it from the parent queue -- `sqs.Queue` has
        # no public getter back to the DLQ it was constructed with.
        self.dlqs[logical_id] = dlq
        return queue
