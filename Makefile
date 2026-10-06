# Prelo - common development tasks. Run `make` (or `make help`) to list them.

IMAGE       ?= prelo:latest
TEST_IMAGE  ?= prelo:test
COMPOSE     ?= docker compose
PYTHON      ?= python3
VENDOR_DIR  ?= vendor
DEV_PORT    ?= 8000
# Extra arguments for `make test`, e.g. make test PYTEST_ARGS="-k security -x"
PYTEST_ARGS ?=

.DEFAULT_GOAL := help
.PHONY: help build up down logs test vendor lock dev clean

help: ## List the available targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z]+:.*## / {printf "  %-7s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

build: ## Build the production image (runtime stage)
	docker build --target runtime -t $(IMAGE) .

up: ## Build and start the service with docker compose
	$(COMPOSE) up -d --build
	@echo "Prelo: http://localhost:$${PRELO_PORT:-8000}  (API: /docs)"

down: ## Stop and remove the container
	$(COMPOSE) down

logs: ## Follow the service logs
	$(COMPOSE) logs -f

test: ## Build the test stage and run the test suite inside Docker
	docker build --target test -t $(TEST_IMAGE) .
	docker run --rm --init --shm-size=512m $(TEST_IMAGE) \
	    $(if $(PYTEST_ARGS),pytest -p no:cacheprovider $(PYTEST_ARGS))

vendor: ## Download Mermaid and Swagger UI into ./vendor (to run without Docker)
	sh scripts/fetch_vendor.sh $(VENDOR_DIR)

lock: ## Pin every Python dependency with hashes (requirements*.in -> requirements*.txt)
	sh scripts/lock_requirements.sh

dev: ## Run locally with auto-reload (needs pandoc, `playwright install chromium`, make vendor)
	PRELO_VENDOR_DIR=$(VENDOR_DIR) $(PYTHON) -m uvicorn app.main:app --reload \
	    --host 127.0.0.1 --port $(DEV_PORT)

clean: ## Remove the project containers/images and local caches
	-$(COMPOSE) down --remove-orphans --rmi all
	-docker image rm $(TEST_IMAGE)
	find . \( -path ./.git -o -path ./.venv -o -path ./venv \) -prune -o -type d \
	    \( -name __pycache__ -o -name .pytest_cache -o -name .ruff_cache \) -prune \
	    -exec rm -rf {} +
