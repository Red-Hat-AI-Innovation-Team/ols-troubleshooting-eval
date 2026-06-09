.PHONY: setup env-up env-down env-nuke eval help

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install OLS + eval dependencies (one-time)
	bash setup.sh

env-up: ## Set up CRC + cache + MCP server (idempotent)
	bash setup_env.sh

env-down: ## Stop CRC (preserves VM for fast restart)
	bash teardown_env.sh

env-nuke: ## Delete CRC + stop cache (full cleanup)
	bash teardown_env.sh --force

eval: ## Run eval (usage: make eval ARGS="<label> <url> <model> [iters]")
	@if [ -z "$(ARGS)" ]; then echo "Usage: make eval ARGS=\"<label> <model_url> <model_name> [iterations]\""; exit 1; fi
	export OPENAI_API_KEY=$$(cat .openai_key) && ./run_eval.sh $(ARGS)
