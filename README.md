# Local Testnet Scripts (Python)

Standalone Python tooling that builds and runs a local MultiversX
testnet from an mx-chain-go checkout: a seednode, N validators and a
proxy wired directly to the validators' REST APIs (no observers), with
one log file per process.

It is a port of the macOS testnet wrapper (`start.sh`, `stop.sh`,
`variables.sh`, `include/common.sh` under `scripts/testnet/mac/` in
mx-chain-go). The shell scripts are untouched and keep working as
before; this is a self-contained reimplementation in clean, idiomatic
Python (standard library only).

## Requirements

- Python 3.8+
- Go toolchain (to build `node`, `seednode`, `filegen`, `proxy`)
- `lsof` (used as a fallback to find processes by port)
- An mx-chain-go checkout (default: sibling `multiversx/mx-chain-go`;
  override with `--mx-chain-go-dir` / `MX_CHAIN_GO_DIR`), plus the sibling source checkouts it references
  (`mx-chain-deploy-go` for `filegen`, `mx-chain-proxy-go` for the
  proxy, `mx-chain-txgen-go` for `make tx-gen`)
- Optional: Klogg (log viewer) for `make klogg`

No Python packages need to be installed (`requirements.txt` is
intentionally dependency-free; tests use `unittest`).

## Installation

Nothing to install. Everything runs from this directory:

```sh
cd mx-local-testnet-scripts-py
```

Optional isolated interpreter (stdlib only, no packages needed):

```sh
python3 -m venv .venv
source .venv/bin/activate
```

## Configuration

Configuration is resolved with the precedence
**CLI flags > environment variables > the chain checkout's
`scripts/testnet/local.sh` > `scripts/testnet/variables.sh`**,
then local-testnet rules are applied (proxy forced on and pointed at
validators, `*:DEBUG` log level, consensus sizes clamped to the validator
counts).

| CLI flag / env var | Default | Meaning |
|---|---|---|
| `--validators` / `NUM_VALIDATORS` | — | validators per shard AND on metachain (shorthand) |
| `--shard-validators` / `SHARD_VALIDATORCOUNT` | 3 | validators per shard (beats the shorthand) |
| `--meta-validators` / `META_VALIDATORCOUNT` | 3 | validators on metachain (beats the shorthand) |
| `--shards` / `SHARDCOUNT` | 3 | number of shards |
| `--proxy-port` / `PORT_PROXY` | 7950 | proxy HTTP port |
| `--log-level` / `LOGLEVEL` | `*:DEBUG` | node/proxy log level |
| `--genesis-delay` / `GENESIS_DELAY` | `30` | seconds added to now for genesis `startTime` |
| `--supernova-round` / `SUPERNOVA_ROUND` | `440` | round in which Supernova activates (rewritten in `config.toml` + `enableRounds.toml`) |

**Note:** `make start` defaults `SUPERNOVA_ROUND` to `50` (see `Makefile`), while
the Python CLI default is `440` (matching the upstream config files). The Makefile
value wins when using `make start`; pass `SUPERNOVA_ROUND=440` explicitly to match
the upstream default, or `python3 src/start.py` directly for the Python default.
| `--rounds-per-epoch` / `ROUNDS_PER_EPOCH` | `0` (`15` via `make start`) | rounds per epoch (`0` = keep node defaults; `15` gives fast epochs) |
| `--node-delay` / `NODE_DELAY` | `10` | seconds to wait after starting the nodes |
| `--testnet-dir` / `TESTNETDIR` | `~/MultiversX/testnet` | where the testnet lives |
| `--mx-chain-go-dir` / `MX_CHAIN_GO_DIR` | auto-detect | mx-chain-go checkout to drive (`multiversx/mx-chain-go` sibling) |

`make` variables map 1:1, e.g. `make start VALIDATORS=2 SHARDS=1`.

Preview the resolved configuration without building anything:

```sh
python3 src/start.py --print-config --validators 2 --shards 1
```

### Validator index mapping

`nodesSetup.json` entries are positional with the **metachain entries
first** (see `processMetaChainAssigment` in the node's
`sharding/nodesSetup.go`). Therefore metachain validators always take
`sk-index` `0..M-1`, followed by shard 0, shard 1, … . `start.py`
assigns indices in that order (`TestnetConfig.validator_slots()`).

The proxy observer list is written shards-first anyway: the proxy asks
its observers **in file order** for the shard count and a metachain
node answers that query with `1`, which aborts proxy startup on
multi-shard testnets. Ports still follow the meta-first mapping, so
every `ShardId` points at a real validator of that shard. Note:
the mac shell script (`scripts/testnet/mac/start.sh` in mx-chain-go)
writes shard entries first too but with un-offset
ports, so its proxy points at the wrong shards; the Python
implementation intentionally diverges here (the shell scripts are
otherwise untouched).

### Startup safety

`start.py` fails fast (before building anything) if any required port
(seednode, proxy, validator p2p/REST) is already held by a process it
does not own — e.g. a stale testnet from an earlier run. PIDs recorded
in live pidfiles are ignored, so repeated starts stay idempotent. If
you hit this, stop the other testnet first (`make stop` with its
`TESTNETDIR=`).

## How to start

```sh
make start                        # defaults: 3 shards x 3 validators + 3 meta
make start VALIDATORS=2 SHARDS=1  # minimal single-shard net
make start SHARD_VALIDATORS=5 META_VALIDATORS=2 LOG_LEVEL='*:INFO'
TESTNETDIR=/tmp/mx-test make start  # scratch directory
```

This builds `filegen`/`seednode`/`node`/`proxy`, generates genesis and
keys, writes configs, then starts the seednode, the validators and the
proxy as detached daemons. Starting twice is safe: already-running
processes are kept.

## How to send transactions (txgen)

```sh
make tx-gen                        # defaults: 250 accounts, basic+erc20+esdt, auto-bulk on
make tx-gen TXGEN_ACCOUNTS=10 TXGEN_SCENARIOS=basic
TXGEN_BULK_ENABLED=0 make tx-gen   # on-demand only, via curl below
```

This builds `mx-chain-txgen-go`, copies its configs under
`<testnet>/txgen` (rewired to the local proxy + funded `walletKey.pem`),
then starts it as a detached daemon (`logs/txgen.log`, `pids/txgen.pid`).
Requires `make start` first. With the default auto-bulk it sends 500 txs
every 10 s; or trigger a batch manually:

```sh
curl -X POST http://127.0.0.1:7951/transaction/send-multiple \
  -H "Content-Type: application/json" -d '{"value":1,"numOfTxs":250,
  "gasPrice":1000000000,"gasLimit":50000,"destination":"mixed",
  "recallNonce":false,"scenario":"basic"}'
```

`make stop` also stops txgen; `make status` lists it.

```sh
make stop-tx-gen     # stop only txgen, leave the testnet running
```

## How to stop

```sh
make stop
```

Stops by pidfile first, then sweeps the known ports with `lsof`.
Safe to run repeatedly. Use the same `TESTNETDIR=` when stopping a
scratch testnet.

## How to check status

```sh
make status
```

Prints one line per expected process (`RUNNING (pid …)` / `STOPPED`)
with per-node details: validators show their role (`meta` / `shard N`),
p2p and REST ports, plus their live `round`/`nonce`/`epoch` probed from
the REST API. The role comes from the node's self-reported shard id when
reachable, so labels stay right even if status runs with different flags
than start; proxy shows its URL, seednode/txgen show no extra detail —
a failed validator/proxy probe (`api DOWN`) still prints and exits 1,
catching processes that are alive but wedged. Probes run
concurrently, so the worst case is ~one timeout, not one per process.

## How to restart a node

```sh
make restart                  # interactive: pick from the node list
make restart NODE=validator2  # restart one node, chain keeps running
```

Restarts a single node gracefully (SIGTERM first) with its existing
configs and working dir, so the chain is untouched. The node list is
discovered from the running pidfiles, so every validator shows up even
when the restart flags differ from the original `make start` topology
(pass the same `SHARDS=`/`VALIDATORS=` for exact shard labels).

```sh
make restart NODE=validator2 SNAPSHOTLESS=1  # snapshotless pruning
```

With `SNAPSHOTLESS=1` the validator is restarted with the node's
`--operation-mode snapshotless-observer` flag (no state snapshots, old
epochs pruned, much less disk use). No config files are modified, so the
setting applies only to that restart; validators restarted without the
flag run normally again.

## How to view logs

```sh
make logs                         # tail of every log file
make log-stats                    # level counts per file + ERROR/WARN lines
make log-stats NODE=validator2    # only one node's log file
make log-stats ERROR_LINES=0 WARN_LINES=0  # counts only, hide lines
```

`log-stats` understands plain lines (`INFO [...]`) and proxy-style
lines (`ERROR[...]`). `launcher.log` (the tool's own output) is always
skipped. Anything without a leading level — Go stack traces, ASCII
tables, `[GIN-debug]` gin chatter — lands in `OTHER`.

- Every process logs to `<testnet>/logs/<name>.log`
  (`validator<N>.log`, `seednode.log`, `proxy.log`, `txgen.log`;
  `launcher.log` captures this tool's own output)

## How to open logs with klogg

```sh
make klogg
```

Opens every generated log file (discovered with a glob, nothing
hardcoded). Uses the `klogg` CLI when it is on your `PATH`, otherwise
the Klogg macOS app (`/Applications/klogg.app`, `glogg.app` as a
fallback). Fails with a clear message if neither is installed or
no logs exist yet.

## How to run tests

```sh
make test                         # python3 -m unittest discover -s tests -v
```

94 tests cover config parsing/precedence/validation, the TOML/JSON
transformations (chain params, CPU flags, staking-v4 recompute, proxy
observer list), and daemon start/stop semantics.  One test is skipped
when the mx-chain-go checkout is absent.

## Project layout

```text
Makefile            start/stop/status/logs/log-stats/clean/klogg/test/tx-gen/stop-tx-gen/restart
requirements.txt    stdlib only — nothing to install
README.md           this file
src/
  config.py         load variables.sh + local.sh from the chain checkout,
                    env/CLI merging,
                    derivation, validation, --print-config rendering
  configure.py      pure file transformations (TOML/JSON edits, staking-v4,
                    proxy observer list) — no I/O
  proc.py           daemon lifecycle: start/stop/pidfiles/lsof port sweep
  files.py          shared filesystem helpers (read/write/copy/copy_glob)
  services.py       shared launchers + validator discovery (used by start,
                    restart and txgen without importing each other)
  start.py          CLI + orchestration (build → generate → configure → launch)
  stop.py           CLI + stop orchestration (also reused by start --clean)
  status.py         CLI status report
  logstats.py       CLI log-level counts per file (used by log-stats)
  restart.py        single-node graceful restart (interactive list or --node)
  txgen.py          CLI + txgen orchestration (build → configure → launch)
tests/
  test_config.py    test_configure.py    test_proc.py    test_restart.py
```
