#!/usr/bin/env bash
# Idempotent: wait for LocalStack to be healthy, then create the DynamoDB
# table, the three S3 buckets, and the three SQS queues (+ DLQs) the backend
# expects, then wait for the backend to answer /health. Run by `make up`
# after `docker compose up` for the core services.
set -euo pipefail

echo "Waiting for LocalStack (s3, sqs, dynamodb) on :4566 ..."
for _ in $(seq 1 60); do
  health="$(curl -sf http://localhost:4566/_localstack/health || true)"
  if echo "$health" | grep -q '"s3": "available"' \
    && echo "$health" | grep -q '"sqs": "available"' \
    && echo "$health" | grep -q '"dynamodb": "available"'; then
    break
  fi
  sleep 2
done

awslocal() {
  docker compose exec -T localstack awslocal "$@"
}

echo "Creating the DynamoDB table (idempotent) ..."
awslocal dynamodb create-table \
  --table-name bookloud-local \
  --attribute-definitions AttributeName=PK,AttributeType=S AttributeName=SK,AttributeType=S \
  --key-schema AttributeName=PK,KeyType=HASH AttributeName=SK,KeyType=RANGE \
  --billing-mode PAY_PER_REQUEST \
  >/dev/null 2>&1 || echo "  (table already exists)"

echo "Creating the S3 buckets (idempotent) ..."
for bucket in bookloud-local-pdfs bookloud-local-audio bookloud-local-marks; do
  awslocal s3 mb "s3://${bucket}" >/dev/null 2>&1 || echo "  (${bucket} already exists)"
done

echo "Creating the SQS queues + DLQs (idempotent) ..."
for base in extract synthesize stitch; do
  awslocal sqs create-queue --queue-name "bookloud-local-${base}-dlq" >/dev/null 2>&1 \
    || echo "  (bookloud-local-${base}-dlq already exists)"
  awslocal sqs create-queue --queue-name "bookloud-local-${base}" >/dev/null 2>&1 \
    || echo "  (bookloud-local-${base} already exists)"
done

echo "Configuring S3 -> SQS notification on the pdf bucket (idempotent) ..."
# S3 -> SQS is long-standing LocalStack *community* functionality (no
# SERVICES change, no Pro token) -- PLANS/phase-3.md §9.1. Mirrors
# infra/stacks/pipeline_stack.py's real notification: prefix "books/", suffix
# ".pdf", targeting the extract queue. put-bucket-notification-configuration
# replaces the whole config each call, so this is naturally idempotent.
awslocal s3api put-bucket-notification-configuration \
  --bucket bookloud-local-pdfs \
  --notification-configuration '{
    "QueueConfigurations": [
      {
        "QueueArn": "arn:aws:sqs:us-east-1:000000000000:bookloud-local-extract",
        "Events": ["s3:ObjectCreated:*"],
        "Filter": {
          "Key": {
            "FilterRules": [
              {"Name": "prefix", "Value": "books/"},
              {"Name": "suffix", "Value": ".pdf"}
            ]
          }
        }
      }
    ]
  }'

echo "Bootstrapping cognito-local (pool/client/dev user) ..."
# The retry *is* the readiness check -- cognito-local's own health endpoint
# isn't worth depending on; just keep hitting the bootstrap script until it
# succeeds (it's idempotent).
cognito_ready=false
for _ in $(seq 1 20); do
  if docker compose exec -T backend uv run python /local-shared/cognito_bootstrap.py; then
    cognito_ready=true
    break
  fi
  sleep 2
done
if [ "$cognito_ready" != "true" ]; then
  echo "ERROR: cognito-local bootstrap did not succeed." >&2
  exit 1
fi

echo "Waiting for the backend on :8000 ..."
for _ in $(seq 1 60); do
  if curl -sf http://localhost:8000/health >/dev/null 2>&1; then break; fi
  sleep 2
done

echo "Local stack ready."
