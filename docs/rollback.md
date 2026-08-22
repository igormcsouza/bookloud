# Rolling back a bad prod deploy

Phase 8 (`IMPLEMENTATION_PLAN.md`). This is the concrete procedure for
undoing a bad `main` push once `deploy-prod.yml` has already deployed it,
not a general principles doc. It matches what is actually in this repo's
CDK code today — see "What's not in place yet" at the bottom for the gap
between this and the more instant alias-based rollback the phase asked
about.

## 0. First, is it actually a deploy problem?

`deploy-prod.yml` runs `ci` → `staging` (deploys `Bookloud*-staging`, seeds
Cognito users, runs the Playwright `chromium` project against it) →
`deploy` (deploys `Bookloud*-prod`, no e2e). If `staging` failed, `deploy`
never ran and prod is untouched — nothing to roll back. If `staging` and
`ci` both passed and `deploy` still produced a broken prod, that's the
scenario this doc covers.

## 1. What "prod" actually is

Five stacks, deployed in this dependency order (`infra/app.py`):

```
BookloudStorage-prod  (DynamoDB table, S3: pdf/audio/marks buckets)
BookloudAuth-prod     (Cognito user pool + client)
BookloudPipeline-prod (extract/synthesize/stitch queues + Lambdas)
BookloudApi-prod      (API Gateway HTTP API + API/chat Lambdas, imports Storage+Auth+Pipeline)
BookloudFrontend-prod (CloudFront + SSR Lambda, imports Api+Auth)
```

Everything compute-shaped (`Pipeline`, `Api`, `Frontend`'s SSR Lambda) is
`DockerImageFunction` built with `DockerImageCode.from_image_asset(...)` —
CDK builds and pushes a **new image tagged from the current source tree**
on every deploy and points the Lambda's `$LATEST` at it. There is no
CDK-published Lambda version and no alias in front of any function today
(see "What's not in place yet"), so **rollback here means redeploying the
previous good commit's CDK app**, not flipping a pointer.

Storage is the one part that is *not* rebuilt: `BookloudStorage-prod` and
`BookloudAuth-prod`'s user pool are `RemovalPolicy.RETAIN` in prod
(`infra/stacks/storage_stack.py`, `infra/stacks/auth_stack.py`), and the
table has point-in-time recovery enabled in prod. A bad app-code deploy
essentially never touches these — the DynamoDB table, PDFs, and Cognito
users all survive a rollback of the compute stacks untouched. The one case
that *does* touch data is a bad **migration** (a change to how `Api`/
`Pipeline` Lambdas read or write table items) — see §4.

## 2. Fast path: revert and redeploy (the supported rollback)

This is the rollback path `deploy-prod.yml` is built for — it re-runs the
exact same gate (`ci` → `staging` e2e → `deploy`) against the known-good
commit, so the rollback itself gets smoke-tested before it touches prod.

```bash
# Find the last good commit -- usually the one before the bad merge.
git log --oneline -10

# Revert the bad commit(s). Prefer `git revert` over a force-push/reset:
# it's a forward-moving commit, safe on a shared branch, and keeps history
# honest about what happened.
git revert --no-edit <bad-commit-sha>
git push origin main
```

Pushing to `main` triggers `deploy-prod.yml` exactly as any other merge
would: unit tests, an ephemeral `staging` deploy + Playwright e2e gate, then
`cdk deploy` of the reverted code to all five `-prod` stacks. Total time is
whatever a normal deploy takes (dominated by the two Docker image builds and
CloudFront's distribution update) plus the staging e2e gate — budget the
same as a normal merge-to-main run, no faster and no slower.

**This is the default recommendation.** It goes through the same gate a
forward deploy does, which is the whole point of phase 8 — a rollback that
skips the e2e gate to save a few minutes is exactly the kind of shortcut
that turns one bad deploy into two.

## 3. Faster path: manual `cdk deploy` from a known-good commit

Use this only when prod is actively broken for users and waiting on a full
CI run (staging deploy + e2e + prod deploy) is not acceptable. It skips the
e2e gate, so treat it as a stopgap — follow up with §2 once things are
stable, so the reverted state is the one that lands back on `main` and gets
the normal gate.

Requires local AWS credentials scoped to the prod account (the same
credentials `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION`/
`AWS_ACCOUNT_ID` secrets that `deploy-prod.yml` uses — pull them from
wherever they're vaulted for out-of-band use, they are not printed by CI).

```bash
git checkout <known-good-sha>

cd infra
pip install -r requirements.txt
npm install -g aws-cdk

# Rebuild the frontend against the KNOWN-GOOD commit's API/chat URLs.
# These don't change between deploys (API Gateway/HTTP API URLs are stable
# across updates to the same stack), so pull them from the last successful
# deploy-prod.yml run's `deploy` job logs ("Extract frontend URL" /
# `cdk-backend-outputs.json`), or:
aws cloudformation describe-stacks --stack-name BookloudApi-prod \
  --query 'Stacks[0].Outputs' --output table

cd ../frontend
npm ci
NEXT_PUBLIC_API_BASE_URL=<api-url> NEXT_PUBLIC_CHAT_BASE_URL=<chat-url> \
  npx open-next build

cd ../infra
cdk deploy \
  "BookloudStorage-prod" "BookloudAuth-prod" \
  "BookloudPipeline-prod" "BookloudApi-prod" \
  --exclusively --require-approval never \
  -c environment=prod -c git_sha="$(git rev-parse HEAD)"

rm -rf cdk.out
cdk deploy "BookloudFrontend-prod" \
  --exclusively --require-approval never \
  -c environment=prod -c git_sha="$(git rev-parse HEAD)"
```

Then run the smoke test against it by hand to confirm:

```bash
python3 local/smoke_test.py \
  --api-url <api-url> --chat-url <chat-url> --frontend-url <frontend-url> \
  --expect-commit "$(git rev-parse HEAD)" --expect-environment prod
```

Once prod is stable again, do the `git revert` from §2 so `main` reflects
reality and the next CI run doesn't silently reintroduce the bad commit.

## 4. If a CloudFormation update itself failed mid-flight

If `cdk deploy` fails partway (not "the new code is bad" but "the stack
update itself errored"), CloudFormation auto-rolls the stack back to its
last good state — this is CloudFormation's own built-in behavior, nothing
this repo has to implement. Confirm and clear it:

```bash
aws cloudformation describe-stacks --stack-name BookloudApi-prod \
  --query 'Stacks[0].StackStatus'
# UPDATE_ROLLBACK_COMPLETE -- CFN already reverted this stack. Re-running
# `cdk deploy` with fixed code will proceed normally from here.
#
# UPDATE_ROLLBACK_FAILED -- the rollback itself couldn't complete (a
# resource wouldn't revert cleanly). Needs:
aws cloudformation continue-update-rollback --stack-name BookloudApi-prod
# then re-check status before attempting another deploy.
```

Do not `cdk destroy` a `-prod` stack to "reset" it. `Storage`/`Auth` are
`RETAIN`, so the data would survive, but `Api`/`Pipeline`/`Frontend` hold
live traffic and re-creating them from scratch means new API Gateway/
CloudFront endpoints — a much bigger outage than the rollback you were
trying to do. The revert-and-redeploy path in §2 always updates the
existing stacks in place.

## 5. A migration (not just code) is the bad deploy

If the bad commit changed how items are read from or written to the
`bookloud-prod` table (an attribute rename, a new required field, a changed
status enum) and forward-only data has already been written by the bad
code, a plain code revert is not enough — the reverted Lambda would then
choke on data shaped by the version you're rolling back from. There's no
generic script for this because it depends entirely on what changed; the
tools available are:

- Point-in-time recovery is enabled on `bookloud-prod`
  (`storage_stack.py`) — `aws dynamodb restore-table-to-point-in-time` can
  restore to immediately before the bad deploy, into a **new** table name
  (PITR restore never overwrites the live table), which you then inspect
  before deciding whether/how to reconcile it back into `bookloud-prod`.
- Prefer a forward-fixing data migration script (read the malformed items,
  rewrite them) over restoring PITR into the live table, since a live
  restore-in-place isn't offered by DynamoDB anyway — restores always land
  in a new table.

Treat this as a case-by-case incident, not a scripted rollback — flag it
distinctly if it comes up, since §2/§3 alone would not fix it.

## What's not in place yet

The phase description mentions "previous Lambda version alias strategy" as
an alternative to a CDK stack rollback. That mechanism does not exist in
this repo's CDK today: none of the `DockerImageFunction`s publish a
versioned snapshot (`fn.current_version`) or sit behind a
`lambda.Alias`/`aws_apigatewayv2_integrations.HttpLambdaIntegration`
pointed at an alias rather than `$LATEST`. Every deploy overwrites
`$LATEST` in place, which is exactly why §2/§3 above are "redeploy the old
code," not "flip a pointer."

Adding it would look like:

1. In each stack that builds a `DockerImageFunction` (`ApiStack`'s `fn` and
   `chat_fn`, and the three functions in `PipelineStack`), publish a version
   and wrap it in an alias:
   ```python
   version = fn.current_version
   alias = lambda_.Alias(self, "ApiFunctionLive", alias_name="live", version=version)
   ```
2. Point every integration at the alias instead of the bare function
   (`apigwv2_integrations.HttpLambdaIntegration(..., alias)` for API Gateway;
   SQS event source mappings would target `alias` too).
3. Rollback then becomes an out-of-band, instant operation with no CDK
   deploy and no rebuild:
   ```bash
   aws lambda list-versions-by-function --function-name <ApiFunction physical name>
   aws lambda update-alias --function-name <name> --name live --function-version <previous-N>
   ```
   repeated per function (`ApiFunction`, `ChatFunction`, the extract/
   synthesize/stitch Lambdas) — there is no single alias covering all of
   them, so a rollback that only rolls back some functions is possible and
   would need to be done deliberately, function by function, when only one
   of them regressed.

This is a real infra change (new CDK constructs, `infra/tests/test_synth.py`
assertions for the alias wiring, a decision on retained-version count via
`removal_policy`/`aws_lambda.Version`), not a config flip, so it's left as
a documented gap rather than bolted on here. §2 (revert + redeploy through
the normal staging-gated pipeline) is the supported rollback until it's
built.
