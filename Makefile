# Local testnet (Python implementation).
#
# Run everything from this directory, e.g.:
#   make start VALIDATORS=2 SHARDS=1
#   make status
#   make stop
#
# Variables (override on the command line or via the environment):
#   TESTNETDIR   where the testnet lives (default: ~/MultiversX/testnet)
#   VALIDATORS   validators per shard AND on metachain
#   SHARDS       number of shards
#   SHARD_VALIDATORS / META_VALIDATORS   fine-grained validator counts
#   LOG_LEVEL    node/proxy log level (default: *:DEBUG)
#   NODE_DELAY   seconds to wait after starting the nodes (default: 10)
#   ROUNDS_PER_EPOCH  rounds per epoch, 0 = keep node defaults (default: 15)
#   NODE         node to restart via `make restart` (e.g. validator2;
#                omit for an interactive list)
#   SNAPSHOTLESS=1  restart the node snapshotless (disable state snapshots,
#                prune old epochs; less disk use)
#   CLEAN_DB=1    restart with a wiped db folder
#                (<testnet>/node_working_dirs/<node>/db is deleted while
#                stopped, so the validator resyncs from scratch)
#   MX_CHAIN_GO_DIR  mx-chain-go checkout to drive (default: auto-detect
#                    sibling multiversx/mx-chain-go)
#
# Txgen variables (for `make tx-gen`; override the same way):
#   TXGEN_PORT           txgen REST port (default: 7951)
#   TXGEN_ACCOUNTS       accounts txgen generates/funds (default: 250)
#   TXGEN_SCENARIOS      e.g. 'basic,erc20,esdt' or '["basic", "esdt"]'
#   TXGEN_BULK_ENABLED   auto-send bulks on a timer (default: 1)
#   TXGEN_BULK_SIZE      txs per bulk (default: 500)
#   TXGEN_BULK_INTERVAL_MS  ms between bulks (default: 10000)
#   TXGENDIR             path to mx-chain-txgen-go/cmd/txgen
SUPERNOVA_ROUND=50
ROUNDS_PER_EPOCH=15
NODE_DELAY=10
SHARDS=3
SHARD_VALIDATORS=3
META_VALIDATORS=3

PYTHON ?= python3
SRC := $(CURDIR)/src

TESTNETDIR ?= $(HOME)/MultiversX/testnet
export TESTNETDIR
# Exported (possibly empty) so stop/status resolve the same checkout.
export MX_CHAIN_GO_DIR

START_ARGS :=
ifdef VALIDATORS
START_ARGS += --validators $(VALIDATORS)
endif
ifdef SHARDS
START_ARGS += --shards $(SHARDS)
endif
ifdef SHARD_VALIDATORS
START_ARGS += --shard-validators $(SHARD_VALIDATORS)
endif
ifdef META_VALIDATORS
START_ARGS += --meta-validators $(META_VALIDATORS)
endif
ifdef LOG_LEVEL
START_ARGS += --log-level $(LOG_LEVEL)
endif
ifdef GENESIS_DELAY
START_ARGS += --genesis-delay $(GENESIS_DELAY)
endif
ifdef SUPERNOVA_ROUND
START_ARGS += --supernova-round $(SUPERNOVA_ROUND)
endif
ifdef ROUNDS_PER_EPOCH
START_ARGS += --rounds-per-epoch $(ROUNDS_PER_EPOCH)
endif
ifdef NODE_DELAY
START_ARGS += --node-delay $(NODE_DELAY)
endif
ifdef MX_CHAIN_GO_DIR
START_ARGS += --mx-chain-go-dir $(MX_CHAIN_GO_DIR)
endif

TXGEN_ARGS :=
ifdef TXGEN_PORT
TXGEN_ARGS += --txgen-port $(TXGEN_PORT)
endif
ifdef TXGEN_ACCOUNTS
TXGEN_ARGS += --txgen-accounts $(TXGEN_ACCOUNTS)
endif
ifdef NUMACCOUNTS
TXGEN_ARGS += --txgen-accounts $(NUMACCOUNTS)
endif
ifdef TXGEN_SCENARIOS
TXGEN_ARGS += --txgen-scenarios $(TXGEN_SCENARIOS)
endif
ifdef TXGENDIR
TXGEN_ARGS += --txgen-dir $(TXGENDIR)
endif
ifdef TXGEN_BULK_ENABLED
TXGEN_ARGS += --txgen-bulk-enabled $(TXGEN_BULK_ENABLED)
endif
ifdef TXGEN_BULK_SIZE
TXGEN_ARGS += --txgen-bulk-size $(TXGEN_BULK_SIZE)
endif
ifdef TXGEN_BULK_INTERVAL_MS
TXGEN_ARGS += --txgen-bulk-interval-ms $(TXGEN_BULK_INTERVAL_MS)
endif
ifdef LOG_LEVEL
TXGEN_ARGS += --log-level $(LOG_LEVEL)
endif
ifdef MX_CHAIN_GO_DIR
TXGEN_ARGS += --mx-chain-go-dir $(MX_CHAIN_GO_DIR)
endif

RESTART_ARGS :=
ifdef NODE
RESTART_ARGS += --node $(NODE)
endif
ifeq ($(SNAPSHOTLESS),1)
RESTART_ARGS += --snapshotless
endif
ifeq ($(CLEAN_DB),1)
RESTART_ARGS += --clean-db
endif
ifdef VALIDATORS
RESTART_ARGS += --validators $(VALIDATORS)
endif
ifdef SHARDS
RESTART_ARGS += --shards $(SHARDS)
endif
ifdef SHARD_VALIDATORS
RESTART_ARGS += --shard-validators $(SHARD_VALIDATORS)
endif
ifdef META_VALIDATORS
RESTART_ARGS += --meta-validators $(META_VALIDATORS)
endif
ifdef MX_CHAIN_GO_DIR
RESTART_ARGS += --mx-chain-go-dir $(MX_CHAIN_GO_DIR)
endif

LOG_FILES = "$(TESTNETDIR)"/logs/*.log

.PHONY: start stop status logs clean klogg test help tx-gen stop-tx-gen restart

start: ## Start the testnet (seednode + validators + proxy).
	$(PYTHON) $(SRC)/start.py $(START_ARGS)

restart: ## Restart a single node (interactive list, or NODE=validator2).
	$(PYTHON) $(SRC)/restart.py $(RESTART_ARGS)

tx-gen: ## Build, configure and start txgen (tx load generator) against the testnet.
	$(PYTHON) $(SRC)/txgen.py $(TXGEN_ARGS)

stop-tx-gen: ## Stop only txgen (leave the testnet running).
	$(PYTHON) $(SRC)/stop.py --only txgen

stop: ## Gracefully stop the testnet.
	$(PYTHON) $(SRC)/stop.py

status: ## Show whether the testnet processes are running.
	$(PYTHON) $(SRC)/status.py

logs: ## Show the tail of every testnet log file.
	@found=0; \
	for f in $(LOG_FILES); do \
		[ -e "$$f" ] || continue; \
		found=1; \
		echo "=== $$f ==="; \
		tail -n 50 "$$f"; \
		echo; \
	done; \
	if [ "$$found" -eq 0 ]; then \
		echo "No log files under $(TESTNETDIR) yet. Run 'make start' first."; \
		exit 1; \
	fi

clean: stop ## Stop the testnet and delete the whole testnet directory.
	@if [ -z "$(TESTNETDIR)" ]; then echo "TESTNETDIR is empty, refusing to clean."; exit 1; fi
	@echo "Removing $(TESTNETDIR)..."
	rm -rf "$(TESTNETDIR)"

klogg: ## Open all testnet log files in klogg.
	@files=""; \
	for f in $(LOG_FILES); do \
		[ -e "$$f" ] && files="$$files $$f"; \
	done; \
	if [ -z "$$files" ]; then \
		echo "No log files under $(TESTNETDIR) yet. Run 'make start' first."; \
		exit 1; \
	fi; \
	if command -v klogg >/dev/null 2>&1; then \
		klogg $$files; \
	elif [ -d "/Applications/klogg.app" ] || [ -d "$(HOME)/Applications/klogg.app" ]; then \
		open -a klogg $$files; \
	elif [ -d "/Applications/glogg.app" ] || [ -d "$(HOME)/Applications/glogg.app" ]; then \
		open -a glogg $$files; \
	else \
		echo "error: klogg is not installed. Install Klogg and make sure it is on your PATH or in /Applications." >&2; \
		exit 1; \
	fi

test: ## Run the unit tests.
	$(PYTHON) -m unittest discover -s tests -v

help: ## Show this help.
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | sed 's/:.*## / - /'
