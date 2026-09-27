# Kraken developer tasks — a thin front-end over the checks in tests/ and
# scripts/. Requires `python3` (stdlib only — no `jq`, no `gh`). `test-e2e`
# drives the real `copilot` and `claude` CLIs against a scripted fake model
# (token-free; each CLI's tests skip when it is not on PATH). `test-agent`
# runs the agent-behavior harness on every agent CLI (one `test-agent-<cli>`
# target each); it needs those CLIs logged in and spends
# tokens, so it is never run automatically (no hook, no CI) — invoke it by
# hand. See CONTRIBUTING.md.
SHELL := bash

.PHONY: help check test test-e2e coverage lint test-agent test-agent-claude test-agent-copilot

help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  make %-19s %s\n", $$1, $$2}'

check: test test-e2e lint ## Run every token-free check (what CI runs on each PR)

test: ## Test suite — conformance + unit, mechanical, token-free (stdlib only)
	python3 -m unittest discover -s tests/unit -p 'test_*.py'
	python3 -m unittest discover -s tests/conformance -p 'test_*.py'

test-e2e: ## Real Copilot CLI + Claude Code vs a scripted fake model — token-free
	python3 -m unittest discover -s tests/e2e -p 'test_*.py'

coverage: ## Line coverage of kraken.py across unit + conformance + e2e, HTML in htmlcov/ (a measurement, not a gate)
	COVERAGE_HTML="$${COVERAGE_HTML:-htmlcov}" bash tests/coverage.sh

lint: ## Deterministic skill lint — token-free
	bash scripts/lint-skills.sh

# A new agent CLI: add its `test-agent-<cli>` target and list it here.
# `make -k test-agent` keeps going past a failing CLI.
test-agent: test-agent-claude test-agent-copilot ## Agent-behavior harness on every agent CLI — REAL model runs, spends tokens

test-agent-claude: ## Agent-behavior harness on Claude Code — REAL model runs, spends tokens
	KRAKEN_AGENT_ASSUME_AUTH=1 KRAKEN_AGENT_CLI=claude bash tests/agent/run-agent-tests.sh

test-agent-copilot: ## Same harness on GitHub Copilot CLI — REAL model runs, spends requests
	KRAKEN_AGENT_ASSUME_AUTH=1 KRAKEN_AGENT_CLI=copilot bash tests/agent/run-agent-tests.sh
