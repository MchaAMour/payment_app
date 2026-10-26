PY ?= .venv/bin/python
CELERY ?= .venv/bin/celery

.PHONY: help setup dev infra migrate run worker listen test lint seed reset-db docker-up docker-down docker-logs docker-test docker-seed prod-up prod-down edge-up edge-down

help:            ## list targets
	@grep -E '^[a-z-]+:.*##' Makefile | sed 's/:.*##/\t/'

setup:           ## create the venv and install dependencies
	python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

dev:             ## everything at once: infra, migrations, web, worker, stripe listener
	./scripts/dev.sh

infra:           ## Postgres + Redis only
	docker compose up -d --wait db redis

migrate: infra   ## apply migrations
	$(PY) manage.py migrate

run: migrate     ## web server
	$(PY) manage.py runserver

worker:          ## Celery worker (retry queue)
	$(CELERY) -A config worker -l info

listen:          ## forward Stripe webhooks to localhost
	stripe listen --events payment_intent.succeeded,payment_intent.payment_failed --forward-to localhost:8000/webhooks/stripe/

test: infra      ## run the test suite (Stripe is mocked)
	$(PY) manage.py test payments

lint:            ## static checks
	$(PY) -m pyflakes payments config

seed:            ## create demo buyer + staff accounts (prints a generated password once)
	$(PY) manage.py seed_demo

reset-db:        ## DESTROY local data and start fresh
	docker compose down -v && $(MAKE) migrate

docker-up:       ## run the whole app in Docker (db, redis, web, worker, stripe listener)
	docker compose --profile app up -d --build --wait db redis web worker
	docker compose --profile app up -d stripe

docker-down:     ## stop the Docker stack (keeps data)
	docker compose --profile app down

docker-logs:     ## follow web, worker and stripe logs
	docker compose --profile app logs -f web worker stripe

docker-test:     ## run the test suite inside the app image
	docker compose --profile app run --rm web python manage.py test payments

docker-seed:     ## create demo accounts inside Docker (prints a generated password once)
	docker compose --profile app exec web python manage.py seed_demo

prod-up:         ## deploy with docker-compose.prod.yml (needs .env.prod)
	docker compose --env-file .env.prod -f docker-compose.prod.yml up -d --build

prod-down:       ## stop the production stack
	docker compose --env-file .env.prod -f docker-compose.prod.yml down

EDGE = docker compose --env-file .env.prod -f docker-compose.prod.yml -f docker-compose.edge.yml

edge-up:         ## deploy behind an existing reverse proxy (docker-compose.edge.yml, needs .env.prod)
	$(EDGE) up -d --build

edge-down:       ## stop the stack started with edge-up
	$(EDGE) down
