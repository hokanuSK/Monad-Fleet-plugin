SHELL := /bin/bash
.DEFAULT_GOAL := help

ifneq (,$(wildcard .env))
include .env
export
endif

COMPOSE ?= docker compose
SMOKE_SCRIPT ?= scripts/smoke/v3_end_to_end_smoke.sh
RESET_SCRIPT ?= scripts/reset/dev_reset.sh

.PHONY: help up build down ps logs smoke smoke-pi reset lint format test verify-layout check

help: ## Show available targets
	@awk 'BEGIN {FS = ":.*## "; print "Targets:"} /^[a-zA-Z0-9_.-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

up: ## Start full local stack (build enabled)
	$(COMPOSE) up -d --build

build: ## Build Fleet and device images only
	$(COMPOSE) build monad-fleet-service model-device

down: ## Stop stack
	$(COMPOSE) down

ps: ## Show service status
	$(COMPOSE) ps

logs: ## Tail Fleet + device logs
	$(COMPOSE) logs -f monad-fleet-service model-device

smoke: ## Run v3 smoke with current environment
	$(SMOKE_SCRIPT)

smoke-pi: ## Run v3 smoke against Pi (override PI_HOST in .env)
	RUN_PI=true PI_HOST=$${PI_HOST:-monad-rpi5.local} $(SMOKE_SCRIPT)

reset: ## Reset Fleet + observability state (keeps MySQL/eLabFTW)
	$(RESET_SCRIPT)

lint: ## Run shell/python/yaml + compose sanity checks
	scripts/dev/lint.sh

format: ## Run optional formatters
	scripts/dev/format.sh

test: ## Run tests and layout verification
	scripts/dev/test.sh

verify-layout: ## Verify canonical vs legacy path contract
	scripts/dev/verify_layout.sh

check: lint test ## Run full local quality gate
