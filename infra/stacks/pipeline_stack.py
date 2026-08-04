import aws_cdk as cdk
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from .config import queue_name


class PipelineStack(cdk.Stack):
    """Extract/synthesize SQS queues + DLQs. Queues only — no consumers yet;
    the extract Lambda (phase 3) and synthesize Lambda (phase 4) attach
    later without a queue-shape change.
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

        self.extract_queue = self._queue_with_dlq("Extract", queue_name("extract", environment))
        self.synthesize_queue = self._queue_with_dlq(
            "Synthesize", queue_name("synthesize", environment)
        )

        cdk.CfnOutput(self, "ExtractQueueUrl", value=self.extract_queue.queue_url)
        cdk.CfnOutput(self, "SynthesizeQueueUrl", value=self.synthesize_queue.queue_url)

    def _queue_with_dlq(self, logical_id: str, name: str) -> sqs.Queue:
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
            visibility_timeout=cdk.Duration.minutes(5),
            retention_period=cdk.Duration.days(4),
            dead_letter_queue=sqs.DeadLetterQueue(max_receive_count=3, queue=dlq),
        )
