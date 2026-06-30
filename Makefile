# Dev shortcuts. Everything runs in the python:3.12-slim image (host Python is broken).
.PHONY: build up down logs test results shell verify fmt

build:        ## build the image
	docker compose build

up:           ## run web + worker (SIMULATE per .env)
	docker compose up

down:
	docker compose down

logs:
	docker compose logs -f --tail=80

test:         ## run the test suite in the container
	docker compose run --rm web python -m pytest

results:      ## (re)load the real-results snapshot into the running board
	curl -fsS -X POST http://localhost:8000/load-results && echo "loaded"

shell:
	docker compose run --rm web bash

verify:       ## environment preflight (host)
	bash scripts/verify_setup.sh
