# Local dev / CI helper targets. `make up` mirrors what deploy-pr.yml and
# deploy-prod.yml prove against real AWS: push code -> synth/deploy -> smoke
# test -> teardown. The client (mobile/, an Expo app) is not part of this
# compose stack -- run it separately with `cd mobile && npx expo start`,
# pointed at the API URLs `make up` prints (issue #10 dropped the Next.js
# `frontend` service this file used to bring up under a `ui` profile).
.PHONY: up seed smoke token logs down test synth

## Build & start LocalStack + cognito-local + backend + extract-worker +
## synthesize-worker + stitch-worker, then create the table/buckets/queues and
## bootstrap the local Cognito pool/client/dev user.
up:
	docker compose up -d --build localstack cognito-local backend chat extract-worker synthesize-worker stitch-worker
	./local/setup.sh

## Re-run local/setup.sh (idempotent).
seed:
	./local/setup.sh

## Run the smoke test against the running local stack. `--expect-synthesis
## silent` matches docker-compose's SYNTHESIS_STUB_MODE=silent on the
## synthesize-worker (PLANS/phase-5.md OQ-1): locally the chunks really do
## synthesize (offline) and the stitcher really does concatenate bytes, so the
## smoke test asserts a READY book with a book.mp3. deploy-pr.yml passes no
## flag and keeps the default `failed` mode.
smoke:
	set -a; . local/.cognito.env; set +a; \
	python3 local/smoke_test.py --api-url http://localhost:8000 \
		--chat-url http://localhost:8001 \
		--cognito-endpoint http://localhost:9229 \
		--cognito-client-id "$$COGNITO_CLIENT_ID" \
		--login-username dev --login-password devpassword \
		--newuser-username newuser --newuser-temp-password 'TempPass123!' \
		--expect-synthesis silent

## Print an id token for the seeded `dev` user, e.g.:
##   curl -H "Authorization: Bearer $(make -s token)" localhost:8000/me
token:
	@set -a; . local/.cognito.env; set +a; \
	python3 -c "\
import json, urllib.request, os; \
req = urllib.request.Request('http://localhost:9229', method='POST', \
    headers={'Content-Type': 'application/x-amz-json-1.1', \
             'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth'}, \
    data=json.dumps({'AuthFlow': 'USER_PASSWORD_AUTH', \
                      'ClientId': os.environ['COGNITO_CLIENT_ID'], \
                      'AuthParameters': {'USERNAME': 'dev', 'PASSWORD': 'devpassword'}}).encode()); \
print(json.load(urllib.request.urlopen(req))['AuthenticationResult']['IdToken'])"

## Tail logs.
logs:
	docker compose logs -f

## Stop and remove everything (including volumes).
down:
	docker compose down -v --remove-orphans

## Run every test suite: backend pytest, mobile jest, infra pytest.
test:
	cd backend && uv run pytest --tb=short
	cd mobile && npm test
	cd infra && pytest tests

## `cdk synth` for local sanity checking.
synth:
	cd infra && cdk synth -c environment=dev
