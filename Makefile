# Kraken developer tasks — a thin front-end over the checks in tests/ and
# scripts/. Requires `python3` + `jq`. `test-agent` additionally needs a
# logged-in `claude` CLI and spends tokens, so it is never run automatically (no
# hook, no CI) — invoke it by hand. See CONTRIBUTING.md.
SHELL := bash

.PHONY: help check test lint test-agent

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  make %-11s %s\n", $$1, $$2}'

check: test lint ## Run every token-free check (what CI runs on each PR)

test: ## Test suite — conformance + unit, mechanical, token-free (needs jq)
	python3 tests/run.py

lint: ## Deterministic skill lint — token-free
	bash scripts/lint-skills.sh

test-agent: ## Agent-behavior harness — REAL model runs, slow, spends tokens
	KRAKEN_AGENT_ASSUME_AUTH=1 bash tests/agent/run-agent-tests.sh
