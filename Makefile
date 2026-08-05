# Local dev / CI helper targets. `make up` mirrors what deploy-pr.yml and
# deploy-prod.yml prove against real AWS: push code -> synth/deploy -> smoke
# test -> teardown.
.PHONY: up ui seed smoke token logs down test synth e2e

## Build & start LocalStack + cognito-local + backend, then create the
## table/buckets/queues and bootstrap the local Cognito pool/client/dev user.
up:
	docker compose up -d --build localstack cognito-local backend
	./local/setup.sh

## Start the Next.js frontend too (http://localhost:3000). Reads the Cognito
## client id local/setup.sh wrote to local/.cognito.env.
ui:
	set -a; . local/.cognito.env; set +a; docker compose --profile ui up -d --build frontend

## Re-run local/setup.sh (idempotent).
seed:
	./local/setup.sh

## Run the smoke test against the running local stack.
smoke:
	set -a; . local/.cognito.env; set +a; \
	python3 local/smoke_test.py --api-url http://localhost:8000 \
		--cognito-endpoint http://localhost:9229 \
		--cognito-client-id "$$COGNITO_CLIENT_ID" \
		--login-username dev --login-password devpassword \
		--newuser-username newuser --newuser-temp-password 'TempPass123!'

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
	docker compose --profile ui down -v --remove-orphans

## Run every test suite: backend pytest, frontend vitest, infra pytest.
test:
	cd backend && uv run pytest --tb=short
	cd frontend && npm test
	cd infra && pytest tests

## `cdk synth` for local sanity checking.
synth:
	cd infra && cdk synth -c environment=dev

## One-shot: bring the stack up (+ui), smoke test both, tear down.
e2e:
	$(MAKE) up
	$(MAKE) ui
	python3 local/smoke_test.py --api-url http://localhost:8000 --frontend-url http://localhost:3000
	$(MAKE) down
