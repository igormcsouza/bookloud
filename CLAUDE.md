# CLAUDE.md

Guidance for anyone (human or agent) working on this repo. For what the app does and how it's built, read `README.md` first -- especially its "Architecture" section (request path, the SQS pipeline, and what to change if SQS request volume becomes a problem again) and "Library context (backend)" (status lifecycle, single-table key patterns, the upload/synthesis/stitch contract). This file is about *working on* the repo, not duplicating what's already documented there.

## Local dev

```bash
make up      # LocalStack + cognito-local + backend + the three pipeline
             # workers (poll loops against LocalStack SQS)
make smoke   # local/smoke_test.py against the running stack -- the closest
             # thing this repo has to an integration/e2e suite; it's also
             # what CI and every deployed environment run
make test    # backend pytest, mobile jest + tsc + eslint, infra CDK
             # assertions synth tests
make synth   # cdk synth -c environment=dev, a local sanity check of every
             # stack without deploying
make down
```

Run the mobile app separately: `cd mobile && npx expo start`, pointed at a running `make up` backend (see `mobile/.env.example` and README's Quickstart for the exact env vars and LAN-IP caveat). There is no per-PR backend deploy to point at instead (issue #35).

Real TTS engines are gated to `ENVIRONMENT=prod` only (see README's "Real TTS runs in prod only"). Don't try to "fix" a `PARTIAL`/`NO_AUDIO` book in local dev or the `staging` CD gate -- that's the expected, deterministic state there, and `make smoke` asserts it.

## Conventions and gotchas worth knowing before changing things

- **Backend layout is `{domain,application,infrastructure,interface}`** per bounded context (DDD-flavoured). `domain/` has no AWS or third-party library imports; look for a Protocol (not an ABC) when adding a new storage/queue/repository dependency.
- **The book status state machine is deliberately asymmetric.** `FAILED` is extraction-only and re-claimable (a stray redelivered S3 event can safely re-extract a `FAILED` book); `PARTIAL` covers "audio incomplete but text is fine" and is terminal, never re-claimable the same way. If you're tempted to add `PARTIAL` to a claimable/reissuable status tuple, don't -- that reintroduces the exact hazard `PARTIAL` exists to prevent (see `test_status_tuples_unchanged` and README's "Repairing a `PARTIAL` book").
- **SQS queues in `infra/stacks/pipeline_stack.py`**: visibility timeout is sized ~6x the consuming Lambda's own timeout -- keep that ratio if you change either. `max_receive_count` + a DLQ bounds retries; only the extract and synthesize DLQs are swept automatically (the stitch DLQ gets an alarm only, see that Lambda's docstring for why). Every queue sets 20s long polling (`receive_message_wait_time`) -- don't remove it, it's what keeps idle SQS request volume off the AWS free tier ceiling.
- **Lambda concurrency is capped account-wide at 10** (small/free-tier account). Don't add `reserved_concurrent_executions` back to the synthesize/stitch Lambdas -- AWS rejects any reservation that would drop unreserved concurrency below its floor. Use each queue's event source mapping `max_concurrency` instead, which throttles without reserving.
- **The Google Cloud TTS fallback secret is provisioned out-of-band, on purpose.** CDK only ever receives the *secret's name*, never its value; the actual API key is created directly in Secrets Manager (see README's "Enabling the Google Cloud TTS fallback"). Don't add a CDK parameter, a GitHub Actions secret, or any other path that would put the key value through source control or CI -- if a task calls for provisioning that key, the secret should be created by hand, not automated.
- **Prod and PR/staging environments deploy to different AWS regions** (prod: `sa-east-1`; PR/staging: `us-east-1`) to keep ephemeral environments off prod's account-level service quotas -- see `infra/app.py`'s comment and README's "Deploy model" before assuming a single-region setup.
- **A bad prod deploy**: see `docs/rollback.md` for the actual procedure; don't improvise one.

## Where design rationale used to live

Earlier development used `IMPLEMENTATION_PLAN.md` and per-phase files under `PLANS/` as working documents while the app was being built phase by phase. Both are gone now that the app is built and those plans are no longer "live" -- their content is preserved in git history if a past decision's full reasoning is ever needed (`git log --all --oneline -- IMPLEMENTATION_PLAN.md PLANS/` finds the commit that last touched them). Going forward, durable design rationale belongs in README.md (architecture-level) or in code comments next to the decision it explains (implementation-level) -- not in a separate planning doc that will eventually stop being read.
