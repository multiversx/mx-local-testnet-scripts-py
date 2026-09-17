#!/usr/bin/env python3
"""Build, configure and start txgen (the transaction generator) against the
local testnet brought up by ``start.py``.

The txgen service mints funded accounts from the testnet ``walletKey.pem``
and then sends transactions through the testnet proxy, either on demand
(``POST /transaction/send-multiple``) or automatically on a timer
(``--scheduled-bulk-enabled``).

Usage:
    python3 src/txgen.py [--print-config] [--no-build] [options]
    make tx-gen [TXGEN_ACCOUNTS=.. TXGEN_SCENARIOS=.. ...]

Requires ``make start`` to have run first (node configs + proxy).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional

import config
import configure
import files
import proc
import services

LOG = logging.getLogger("txgen")

# mx-chain-txgen-go is a private repo: txgen is an optional load generator,
# never required by start/stop/status/restart. Missing-checkout errors say
# so explicitly, so users without access know they can just skip it.
_TXGEN_OPTIONAL_NOTE = (
    "Txgen is optional (private repo) -- the testnet runs fine without "
    "it; skip `make tx-gen` if you don't have access."
)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start txgen against the local testnet.")
    parser.add_argument("--txgen-port", default=None,
                        help="txgen REST port (default: 7951)")
    parser.add_argument("--txgen-accounts", default=None,
                        help="accounts to generate (default: 250, env NUMACCOUNTS)")
    parser.add_argument("--txgen-scenarios", default=None,
                        help='e.g. "basic,erc20,esdt" or \'["basic", "esdt"]\'')
    parser.add_argument("--txgen-dir", default=None,
                        help="path to mx-chain-txgen-go/cmd/txgen")
    parser.add_argument("--txgen-bulk-enabled", default=None,
                        help="auto-send bulks on a timer (default: on)")
    parser.add_argument("--txgen-bulk-size", default=None)
    parser.add_argument("--txgen-bulk-interval-ms", default=None)
    parser.add_argument("--proxy-port", default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--testnet-dir", default=None)
    parser.add_argument("--mx-chain-go-dir", default=None,
                        help="mx-chain-go checkout to drive "
                             "(default: auto-detect sibling checkouts)")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    return parser.parse_args(argv)


def _read(path: str) -> str:
    # Backward-compat alias (new code uses files.read directly).
    return files.read(path)


def _write(path: str, text: str) -> None:
    # Backward-compat alias (new code uses files.write directly).
    files.write(path, text)


def _copy(src: str, dst: str) -> None:
    # Backward-compat alias (new code uses files.copy directly).
    files.copy(src, dst)


def _copy_glob(pattern: str, dst_dir: str) -> None:
    # Backward-compat alias (new code uses files.copy_glob directly).
    files.copy_glob(pattern, dst_dir)


def _assert_proxy_up(cfg: config.TestnetConfig) -> None:
    """Fail fast when the testnet proxy is not running.

    Without a proxy txgen cannot fetch the network config or submit
    transactions, so it would die during minting with a cryptic error.
    """
    pidfile = os.path.join(cfg.pid_dir, "proxy.pid")
    pid = proc.read_pidfile(pidfile)
    if pid is not None and proc.is_running(pid):
        return
    if proc.pids_on_port(cfg.proxy_port):
        return
    raise proc.DaemonError(
        "proxy is not running (port %d). Run `make start` first, then `make tx-gen`."
        % cfg.proxy_port
    )


def _assert_txgen_port_free(cfg: config.TestnetConfig) -> None:
    own_pids = set()
    pidfile = os.path.join(cfg.pid_dir, "txgen.pid")
    pid = proc.read_pidfile(pidfile)
    if pid is not None and proc.is_running(pid):
        own_pids.add(pid)
    strangers = [p for p in proc.pids_on_port(cfg.txgen_port) if p not in own_pids]
    if strangers:
        raise proc.DaemonError(
            "cannot start txgen: port %d already in use (pids %s)."
            % (cfg.txgen_port, ",".join(map(str, strangers)))
        )


def build_txgen(cfg: config.TestnetConfig) -> None:
    if not os.path.isdir(cfg.txgen_dir):
        raise proc.DaemonError(
            "%s. %s"
            % (
                config.missing_repo_message(
                    cfg.txgen_dir,
                    config.REPO_TXGEN_GO,
                    config.sibling_clone_dest(
                        cfg.repo_root, config.REPO_TXGEN_GO),
                    "set TXGENDIR / --txgen-dir to its cmd/txgen dir",
                ),
                _TXGEN_OPTIONAL_NOTE,
            )
        )
    LOG.info("Building txgen...")
    proc.run(["go", "build", "-o", cfg.txgen_bin, "."], cfg.txgen_dir)


def setup_txgen(cfg: config.TestnetConfig) -> None:
    """Copy + rewire the txgen configs (mirrors ``copyTxGenConfig``)."""
    if not os.path.isdir(cfg.txgen_dir):
        raise proc.DaemonError(
            "%s. %s"
            % (
                config.missing_repo_message(
                    cfg.txgen_dir,
                    config.REPO_TXGEN_GO,
                    config.sibling_clone_dest(
                        cfg.repo_root, config.REPO_TXGEN_GO),
                    "set TXGENDIR / --txgen-dir to its cmd/txgen dir",
                ),
                _TXGEN_OPTIONAL_NOTE,
            )
        )
    node_cfg = os.path.join(cfg.testnet_dir, "node", "config")
    for name in ("economics.toml", "walletKey.pem", "enableEpochs.toml"):
        if not os.path.isfile(os.path.join(node_cfg, name)):
            raise proc.DaemonError(
                "missing %s in testnet node config. Run `make start` first."
                % name
            )
    txgen_cfg = os.path.join(cfg.testnet_dir, "txgen", "config")
    node_cfg_mirror = os.path.join(txgen_cfg, "nodeConfig", "config")
    os.makedirs(node_cfg_mirror, exist_ok=True)

    src_cfg = os.path.join(cfg.txgen_dir, "config")
    if not os.path.isfile(os.path.join(src_cfg, "config.toml")):
        raise proc.DaemonError(
            "missing config.toml in %s (txgen checkout at %s looks "
            "incomplete). %s. %s"
            % (
                src_cfg,
                cfg.txgen_dir,
                config.git_clone_hint(
                    config.REPO_TXGEN_GO,
                    config.sibling_clone_dest(
                        cfg.repo_root, config.REPO_TXGEN_GO),
                ),
                _TXGEN_OPTIONAL_NOTE,
            )
        )
    files.copy(os.path.join(src_cfg, "config.toml"), txgen_cfg)
    files.copy(os.path.join(src_cfg, "sc.toml"), txgen_cfg)
    files.copy_glob(os.path.join(src_cfg, "*.wasm"), txgen_cfg)

    files.copy(os.path.join(node_cfg, "economics.toml"), txgen_cfg)
    files.copy(os.path.join(node_cfg, "walletKey.pem"), txgen_cfg)
    files.copy(os.path.join(node_cfg, "enableEpochs.toml"), node_cfg_mirror)

    config_toml = os.path.join(txgen_cfg, "config.toml")
    files.write(
        config_toml,
        configure.apply_txgen_config(
            files.read(config_toml), cfg.txgen_port, cfg.proxy_port,
            cfg.txgen_scenarios,
        ),
    )
    LOG.info(
        "Txgen wired to proxy http://127.0.0.1:%d with scenarios [%s].",
        cfg.proxy_port, ",".join(cfg.txgen_scenarios),
    )


def _txgen_argv(cfg: config.TestnetConfig) -> List[str]:
    # Backward-compat alias: canonical implementation lives in services.
    return services.txgen_argv(cfg)


def launch_txgen(cfg: config.TestnetConfig) -> int:
    # Thin wrapper so existing callers (restart, tests) keep working;
    # canonical implementation lives in services.
    return services.launch_txgen(cfg)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = config.load_config(
            {
                "txgen_port": args.txgen_port,
                "txgen_accounts": args.txgen_accounts,
                "txgen_scenarios": args.txgen_scenarios,
                "txgen_dir": args.txgen_dir,
                "txgen_bulk_enabled": args.txgen_bulk_enabled,
                "txgen_bulk_size": args.txgen_bulk_size,
                "txgen_bulk_interval_ms": args.txgen_bulk_interval_ms,
                "proxy_port": args.proxy_port,
                "log_level": args.log_level,
                "testnet_dir": args.testnet_dir,
                "mx_chain_go_dir": args.mx_chain_go_dir,
            }
        )
    except config.ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    print(
        "=== txgen: port=%d accounts=%d scenarios=[%s] bulk=%s proxy=:%d ==="
        % (
            cfg.txgen_port,
            cfg.txgen_num_accounts,
            ",".join(cfg.txgen_scenarios),
            "on (%d txs / %d ms)" % (cfg.txgen_bulk_size, cfg.txgen_bulk_interval_ms)
            if cfg.txgen_bulk_enabled else "off",
            cfg.proxy_port,
        )
    )
    if args.print_config:
        print(config.format_print_config(cfg))
        return 0

    os.makedirs(cfg.log_dir, exist_ok=True)
    file_handler = logging.FileHandler(
        os.path.join(cfg.log_dir, "launcher.log"), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"))
    logging.getLogger().addHandler(file_handler)

    try:
        for directory in (
            os.path.join(cfg.testnet_dir, "txgen"),
            os.path.join(cfg.testnet_dir, "txgen", "config"),
            os.path.join(cfg.testnet_dir, "txgen", "config", "nodeConfig", "config"),
            cfg.pid_dir,
            cfg.log_dir,
        ):
            os.makedirs(directory, exist_ok=True)

        _assert_proxy_up(cfg)
        _assert_txgen_port_free(cfg)
        if not args.no_build:
            build_txgen(cfg)
        else:
            LOG.info("Skipping build (--no-build).")
        setup_txgen(cfg)
        pid = launch_txgen(cfg)
    except (proc.DaemonError, config.ConfigError, OSError) as exc:
        LOG.error("%s", exc)
        print("error: %s" % exc, file=sys.stderr)
        return 1

    print("")
    print("=== txgen up (pid %d) ===" % pid)
    print("REST:    http://127.0.0.1:%d/transaction/status" % cfg.txgen_port)
    print("Logs:    %s/txgen.log   Pids: %s/txgen.pid"
          % (cfg.log_dir, cfg.pid_dir))
    print("Send transactions:")
    print('  curl -X POST http://127.0.0.1:%d/transaction/send-multiple \\'
          % cfg.txgen_port)
    print('    -H "Content-Type: application/json" -d \'{"value":1,')
    print('    "numOfTxs":250,"gasPrice":1000000000,"gasLimit":50000,')
    print('    "destination":"mixed","recallNonce":false,"scenario":"basic"}\'')
    print("Stop with: make stop (or python3 src/stop.py)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    sys.exit(main())
