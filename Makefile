# Local dev / CI helper targets. `make up` mirrors what deploy-pr.yml and
# deploy-prod.yml prove against real AWS: push code -> synth/deploy -> smoke
# test -> teardown.
.PHONY: up ui seed smoke logs down test synth e2e

## Build & start LocalStack + backend, then create the table/buckets/queues.
up:
	docker compose up -d --build localstack backend
	./local/setup.sh

## Start the Next.js frontend too (http://localhost:3000).
ui:
	docker compose --profile ui up -d --build frontend

## Re-run local/setup.sh (idempotent).
seed:
	./local/setup.sh

## Run the smoke test against the running local stack.
smoke:
	python3 local/smoke_test.py --api-url http://localhost:8000

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
