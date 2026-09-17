#!/usr/bin/env python3
"""Bring up a local testnet: seednode + validators + proxy.

Python port of ``scripts/testnet/mac/start.sh``. All daemons run detached
with one log file each; PIDs are recorded under ``<testnet>/pids``.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import signal
import sys
import time
from typing import List, Optional, Tuple

import config
import configure
import files
import proc
import services
import stop

LOG = logging.getLogger("start")

_STARTED: List[Tuple[str, str]] = []


def _on_signal(signum: int, _frame: object) -> None:
    LOG.warning("received signal %d, stopping started daemons...", signum)
    for name, pidfile in reversed(_STARTED):
        try:
            proc.stop_by_pidfile(name, pidfile)
        except Exception as exc:  # never fail inside a signal handler
            LOG.warning("error stopping %s: %s", name, exc)
    sys.exit(128 + signum)


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Start a local Mac testnet.")
    parser.add_argument("--validators", default=None,
                        help="validators per shard AND on metachain")
    parser.add_argument("--shard-validators", default=None)
    parser.add_argument("--meta-validators", default=None)
    parser.add_argument("--shards", default=None)
    parser.add_argument("--proxy-port", default=None)
    parser.add_argument("--log-level", default=None)
    parser.add_argument("--genesis-delay", default=None,
                        help="seconds added to now for genesis startTime "
                             "(default: 30)")
    parser.add_argument("--supernova-round", default=None,
                        help="round in which Supernova activates "
                             "(default: 440)")
    parser.add_argument("--rounds-per-epoch", default=None,
                        help="rounds per epoch (0 = keep node defaults, "
                             "e.g. 15 for fast epochs)")
    parser.add_argument("--node-delay", default=None,
                        help="seconds to wait after starting the nodes "
                             "(default: 10)")
    parser.add_argument("--testnet-dir", default=None)
    parser.add_argument("--mx-chain-go-dir", default=None,
                        help="mx-chain-go checkout to drive "
                             "(default: auto-detect sibling checkouts)")
    parser.add_argument("--no-build", action="store_true")
    parser.add_argument("--clean", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    return parser.parse_args(argv)


def _copy(src: str, dst: str) -> None:
    # Backward-compat alias (new code uses files.copy directly).
    files.copy(src, dst)


def _copy_glob(pattern: str, dst_dir: str) -> None:
    # Backward-compat alias (new code uses files.copy_glob directly).
    files.copy_glob(pattern, dst_dir)


def _read(path: str) -> str:
    # Backward-compat alias (new code uses files.read directly).
    return files.read(path)


def _write(path: str, text: str) -> None:
    # Backward-compat alias (new code uses files.write directly).
    files.write(path, text)


def _edit(path: str, text: str) -> None:
    files.edit(path, text)


def build_all(cfg: config.TestnetConfig) -> None:
    LOG.info("Building filegen...")
    proc.run(["go", "build", "-o", cfg.filegen_bin, "."], cfg.filegen_dir)
    LOG.info("Building seednode...")
    proc.run(["go", "build", "-o", cfg.seednode_bin, "."], cfg.seednode_dir)
    LOG.info("Building node...")
    proc.run(
        [
            "go", "build", "-gcflags=all=-N -l",
            "-ldflags=-X main.appVersion=%s" % config.APP_VERSION,
            "-o", cfg.node_bin, ".",
        ],
        cfg.node_dir,
    )
    LOG.info("Building proxy...")
    proc.run(["go", "build", "-o", cfg.proxy_bin, "."], cfg.proxy_dir)


def generate_config(cfg: config.TestnetConfig) -> None:
    LOG.info("Generating configuration...")
    proc.run(
        [
            "./filegen",
            "-output-directory", cfg.filegen_output_dir,
            "-num-of-shards", str(cfg.shard_count),
            "-num-of-nodes-in-each-shard", str(cfg.shard_validator_count),
            "-num-of-observers-in-each-shard", "0",
            "-consensus-group-size", str(cfg.shard_consensus_size),
            "-num-of-metachain-nodes", str(cfg.meta_validator_count),
            "-num-of-observers-in-metachain", "0",
            "-metachain-consensus-group-size", str(cfg.meta_consensus_size),
            "-stake-type", cfg.genesis_stake_type,
            "-hysteresis", repr(cfg.hysteresis),
            "-round-duration", str(cfg.round_duration_ms),
        ],
        os.path.dirname(cfg.filegen_bin),
    )


def setup_seednode(cfg: config.TestnetConfig) -> str:
    """Write the seednode config; return its p2p multiaddress."""
    seed_cfg = os.path.join(cfg.testnet_dir, "seednode", "config")
    seed_src_cfg = os.path.join(cfg.seednode_dir, "config")
    for name in os.listdir(seed_src_cfg):
        files.copy(os.path.join(seed_src_cfg, name),
              os.path.join(seed_cfg, name))
    keygen_pem = os.path.join(cfg.keygen_dir, "p2pKey.pem")
    if not os.path.isfile(keygen_pem):
        proc.run(["go", "build", "."], cfg.keygen_dir)
        proc.run(["./keygenerator", "--key-type", "p2p"], cfg.keygen_dir)
    files.copy(keygen_pem, os.path.join(seed_cfg, "p2pKey.pem"))
    with open(os.path.join(seed_cfg, "p2pKey.pem"), encoding="utf-8") as handle:
        pubkey = configure.p2p_pubkey_from_first_line(handle.readline())
    address = "/ip4/%s/tcp/%d/p2p/%s" % (cfg.seednode_ip, cfg.seednode_port, pubkey)
    LOG.info("Seednode: %s", address)
    p2p_toml = os.path.join(seed_cfg, "p2p.toml")
    files.edit(p2p_toml, configure.set_toml_value(files.read(p2p_toml), "Port", '"%d"' % cfg.seednode_port))
    return address


def setup_node_config(cfg: config.TestnetConfig, seed_address: str) -> None:
    node_cfg = os.path.join(cfg.testnet_dir, "node", "config")
    src_cfg = os.path.join(cfg.node_dir, "config")
    files.copy(os.path.join(src_cfg, "api.toml"), node_cfg)
    files.copy(os.path.join(src_cfg, "config.toml"),
          os.path.join(node_cfg, "config_validator.toml"))
    for name in ("economics.toml", "ratings.toml", "prefs.toml", "external.toml",
                 "p2p.toml", "fullArchiveP2P.toml", "enableEpochs.toml",
                 "enableRounds.toml", "systemSmartContractsConfig.toml",
                 "genesisSmartContracts.json"):
        files.copy(os.path.join(src_cfg, name), node_cfg)
    for subdir in ("genesisContracts", "gasSchedules"):
        dst = os.path.join(node_cfg, subdir)
        os.makedirs(dst, exist_ok=True)
        files.copy_glob(os.path.join(src_cfg, subdir, "*.*"), dst)

    out_dir = os.path.join(cfg.testnet_dir, "filegen", cfg.filegen_output_dir)
    for name in ("genesis.json", "nodesSetup.json"):
        files.copy(os.path.join(out_dir, name), node_cfg)
    files.copy_glob(os.path.join(out_dir, "*.pem"), node_cfg)

    p2p_toml = os.path.join(node_cfg, "p2p.toml")
    files.edit(p2p_toml, configure.set_toml_value(
        files.read(p2p_toml), "InitialPeerList", '["%s"]' % seed_address))

    nodes_setup = os.path.join(node_cfg, "nodesSetup.json")
    start_time = int(time.time()) + cfg.genesis_delay
    edited = configure.set_json_value(files.read(nodes_setup), "startTime", str(start_time))
    edited = configure.set_json_value(edited, "minTransactionVersion", '"1"')
    files.edit(nodes_setup, edited)

    validator_toml = os.path.join(node_cfg, "config_validator.toml")
    if cfg.always_new_chainid == 1:
        files.edit(validator_toml, configure.set_toml_value(
            files.read(validator_toml), "ChainID", '"local-testnet"'))
    edited, warnings = configure.apply_chain_params(
        files.read(validator_toml),
        cfg.shard_consensus_size,
        cfg.meta_consensus_size,
        cfg.round_duration_ms,
        cfg.hysteresis,
        cfg.rounds_per_epoch,
    )
    for warning in warnings:
        LOG.warning("config_validator.toml: %s", warning)
    files.edit(validator_toml, edited)
    files.edit(validator_toml, configure.empty_cpu_flags(files.read(validator_toml)))
    files.edit(validator_toml, configure.enable_db_lookup_extension(files.read(validator_toml)))
    edited, warnings = configure.apply_supernova_round(
        files.read(validator_toml), cfg.supernova_round)
    for warning in warnings:
        LOG.warning("config_validator.toml: %s", warning)
    files.edit(validator_toml, edited)

    epochs_toml = os.path.join(node_cfg, "enableEpochs.toml")
    updated, message = configure.update_staking_v4_max_nodes(
        files.read(epochs_toml), cfg.shard_count)
    LOG.info(message)
    files.edit(epochs_toml, updated)

    rounds_toml = os.path.join(node_cfg, "enableRounds.toml")
    files.edit(rounds_toml, configure.apply_supernova_enable_round(
        files.read(rounds_toml), cfg.supernova_round))


def setup_proxy(cfg: config.TestnetConfig) -> None:
    proxy_cfg = os.path.join(cfg.testnet_dir, "proxy", "config")
    node_cfg = os.path.join(cfg.testnet_dir, "node", "config")
    src_cfg = os.path.join(cfg.proxy_dir, "config")
    shutil.copytree(os.path.join(src_cfg, "apiConfig"),
                    os.path.join(proxy_cfg, "apiConfig"), dirs_exist_ok=True)
    files.copy(os.path.join(src_cfg, "config.toml"), proxy_cfg)
    for name in ("economics.toml", "external.toml", "walletKey.pem"):
        files.copy(os.path.join(node_cfg, name), proxy_cfg)

    proxy_toml = os.path.join(proxy_cfg, "config.toml")
    truncated = configure.truncate_proxy_observers(files.read(proxy_toml))
    edited = configure.set_toml_value(truncated, "ServerPort", str(cfg.proxy_port))
    edited += configure.render_proxy_observers(
        cfg.validator_slots(), cfg.validator_rest_origin)
    files.edit(proxy_toml, edited)
    LOG.info("Proxy wired to %d validator REST APIs.", cfg.validator_count())


def assert_ports_free(cfg: config.TestnetConfig) -> None:
    """Fail fast when required ports are held by foreign processes.

    Without this, a stale testnet squatting the ports would leave the new
    one half-started (e.g. seednode dead on bind, proxy misreading shards).
    PIDs recorded in our own live pidfiles are ignored so that repeated
    starts stay idempotent.
    """
    own_pids = set()
    if os.path.isdir(cfg.pid_dir):
        for pidfile in os.listdir(cfg.pid_dir):
            if not pidfile.endswith(".pid"):
                continue
            pid = proc.read_pidfile(os.path.join(cfg.pid_dir, pidfile))
            if pid is not None and proc.is_running(pid):
                own_pids.add(pid)
    needed = (
        [cfg.seednode_port, cfg.proxy_port]
        + [cfg.validator_p2p_port(i) for i in range(cfg.validator_count())]
        + [cfg.validator_rest_port(i) for i in range(cfg.validator_count())]
    )
    busy = []
    by_port = proc.pids_by_port()
    for port in needed:
        strangers = [p for p in by_port.get(port, []) if p not in own_pids]
        if strangers:
            busy.append("port %d (pids %s)" % (port, ",".join(map(str, strangers))))
    if busy:
        raise proc.DaemonError(
            "cannot start: %s already in use. Stop the other testnet first "
            "(make stop with its TESTNETDIR) or pick different ports."
            % "; ".join(busy)
        )


def _node_argv(cfg: config.TestnetConfig, port: int, rest_port: int,
               index: int, workdir: str, snapshotless: bool = False) -> List[str]:
    # Backward-compat alias: canonical implementation lives in services.
    return services.node_argv(cfg, port, rest_port, index, workdir,
                              snapshotless=snapshotless)


def _track(name: str, pidfile: str) -> None:
    _STARTED.append((name, pidfile))


def any_testnet_running(cfg: config.TestnetConfig) -> bool:
    """Return True if any pidfile in ``cfg.pid_dir`` points at a live process.

    Used to keep restarts safe: ``generate_config`` creates a brand-new
    genesis on every start (new random validator keys via filegen plus a new
    ``startTime``). Reusing old validator DBs (``node_working_dirs``) with a
    new genesis makes nodes reject each other's headers
    (``checkGenesisTimeForHeaderBeforeSupernova: genesis time mismatch``) and
    fail signature checks, so the network never proposes blocks. This mirrors
    ``reset.sh`` (stop + clean before ``config.sh``): only reuse working dirs
    when we also reuse the configs (idempotent start while already up).
    """
    if not os.path.isdir(cfg.pid_dir):
        return False
    for pidfile in os.listdir(cfg.pid_dir):
        if not pidfile.endswith(".pid"):
            continue
        pid = proc.read_pidfile(os.path.join(cfg.pid_dir, pidfile))
        if pid is not None and proc.is_running(pid):
            return True
    return False


def launch_all(cfg: config.TestnetConfig) -> int:
    services.launch_seednode(cfg)
    _track("seednode", os.path.join(cfg.pid_dir, "seednode.pid"))
    LOG.info("Waiting for the seednode (%d s)...", cfg.seednode_delay)
    time.sleep(cfg.seednode_delay)

    # Metachain validators take the first indices (see validator_slots):
    # nodesSetup.json entries are positional with metachain first.
    count = 0
    for index, _kind, _shard in cfg.validator_slots():
        _launch_validator(cfg, index)
        count += 1
    LOG.info("Waiting for the nodes (%d s)...", cfg.node_delay)
    time.sleep(cfg.node_delay)

    services.launch_proxy(cfg)
    _track("proxy", os.path.join(cfg.pid_dir, "proxy.pid"))
    LOG.info("Waiting for the proxy (%d s)...", cfg.proxy_delay)
    time.sleep(cfg.proxy_delay)
    return count


def _launch_validator(cfg: config.TestnetConfig, index: int,
                      snapshotless: bool = False) -> None:
    # Thin wrapper: canonical implementation lives in services (so
    # restart.py no longer needs to import this CLI module). Tracking
    # stays here because _STARTED belongs to the start run's signal handler.
    services.launch_validator(cfg, index, snapshotless=snapshotless)
    _track("validator%d" % index, os.path.join(cfg.pid_dir, "validator%d.pid" % index))


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = config.load_config(
            {
                "validators": args.validators,
                "shards": args.shards,
                "shard_validators": args.shard_validators,
                "meta_validators": args.meta_validators,
                "proxy_port": args.proxy_port,
                "log_level": args.log_level,
                "genesis_delay": args.genesis_delay,
                "supernova_round": args.supernova_round,
                "rounds_per_epoch": args.rounds_per_epoch,
                "node_delay": args.node_delay,
                "testnet_dir": args.testnet_dir,
                "mx_chain_go_dir": args.mx_chain_go_dir,
            }
        )
    except config.ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    print(
        "=== macOS testnet: shards=%d shard_validators=%d "
        "meta_validators=%d proxy=:%d log=%s ==="
        % (cfg.shard_count, cfg.shard_validator_count,
           cfg.meta_validator_count, cfg.proxy_port, cfg.loglevel)
    )
    if args.print_config:
        print(config.format_print_config(cfg))
        return 0

    os.makedirs(cfg.log_dir, exist_ok=True)
    file_handler = logging.FileHandler(
        os.path.join(cfg.log_dir, "launcher.log"), encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s"))
    root = logging.getLogger()
    root.addHandler(file_handler)

    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, _on_signal)

    try:
        if args.clean:
            LOG.info("Cleaning %s...", cfg.testnet_dir)
            stop.stop_all(cfg)
            shutil.rmtree(cfg.testnet_dir, ignore_errors=True)

        for directory in (
            cfg.testnet_dir,
            os.path.join(cfg.testnet_dir, "filegen"),
            os.path.join(cfg.testnet_dir, "node", "config"),
            os.path.join(cfg.testnet_dir, "seednode", "config"),
            os.path.join(cfg.testnet_dir, "node_working_dirs"),
            os.path.join(cfg.testnet_dir, "proxy", "config"),
            cfg.pid_dir,
            cfg.log_dir,
        ):
            os.makedirs(directory, exist_ok=True)

        assert_ports_free(cfg)

        already_running = any_testnet_running(cfg)
        if already_running:
            LOG.info(
                "Testnet already running, keeping existing configs "
                "(skipping regeneration to preserve the running chain)."
            )
        else:
            # Fresh start (first boot or restart after stop): drop stale
            # validator DBs. generate_config() below creates a new genesis
            # (new keys + new startTime); old DBs would mismatch it and nodes
            # would never propose blocks.
            working_dirs = os.path.join(cfg.testnet_dir, "node_working_dirs")
            if os.path.isdir(working_dirs):
                LOG.info(
                    "Cleaning stale working dirs %s for fresh genesis...",
                    working_dirs,
                )
                shutil.rmtree(working_dirs, ignore_errors=True)
                os.makedirs(working_dirs, exist_ok=True)

            if not args.no_build:
                build_all(cfg)
            else:
                LOG.info("Skipping build (--no-build).")

            generate_config(cfg)
            seed_address = setup_seednode(cfg)
            setup_node_config(cfg, seed_address)
            setup_proxy(cfg)
        count = launch_all(cfg)
    except (proc.DaemonError, config.ConfigError, OSError) as exc:
        LOG.error("%s", exc)
        return 1

    print("")
    print("=== macOS testnet up ===")
    print("Seednode:  127.0.0.1:%d" % cfg.seednode_port)
    print("Proxy:     http://127.0.0.1:%d" % cfg.proxy_port)
    print("Nodes:     %d validators (shards=%d, per-shard=%d, meta=%d)"
          % (count, cfg.shard_count, cfg.shard_validator_count,
             cfg.meta_validator_count))
    print("REST APIs: localhost:%d..%d"
          % (cfg.validator_rest_origin,
             cfg.validator_rest_origin + count - 1))
    print("Logs:      %s   Pids: %s"
          % (cfg.log_dir, cfg.pid_dir))
    print("Stop with: make stop (or python3 src/stop.py)")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    sys.exit(main())
