#!/usr/bin/env python3
"""Shared service launchers + validator discovery (no CLI, no signals).

Single home for the helpers previously spread across ``start.py``,
``txgen.py`` and ``restart.py`` so CLI modules never import each other:

* ``start.py`` owned ``_node_argv`` / ``_launch_validator`` but
  ``restart.py`` needed them (deferred ``import start``).
* ``txgen.py`` owned ``launch_txgen`` but ``restart.py`` needed it
  (deferred ``import txgen``).
* ``restart.py`` owned pidfile-based validator discovery duplicated
  in spirit by ``status.py``.

Everything here operates on a resolved ``TestnetConfig`` and uses
``proc.start_daemon``; signal handling / ``_STARTED`` tracking stays in
the ``start`` CLI.
"""

from __future__ import annotations

import os
import time
from typing import Dict, List, Tuple

import config
import proc


def node_argv(
    cfg: config.TestnetConfig,
    port: int,
    rest_port: int,
    index: int,
    workdir: str,
    snapshotless: bool = False,
) -> List[str]:
    argv = [
        "./node",
        "-port", str(port),
        "--profile-mode",
        "-log-save",
        "-log-level", cfg.loglevel,
        "--log-logger-name",
        "--log-correlation",
        "--use-health-service",
        "-rest-api-interface", "localhost:%d" % rest_port,
        "-sk-index", str(index),
        "-working-directory", workdir,
        "-config", "./config/config_validator.toml",
        # Plain log lines (no ANSI color codes): the logs stay grep-able
        # and trivially parseable (see logstats.py). Seednode/proxy do
        # not define this flag, so it is only passed to the node binary.
        "--disable-ansi-color",
    ]
    if snapshotless:
        argv += ["--operation-mode", "snapshotless-observer"]
    if cfg.node_niceness is not None:
        argv = ["nice", "-n", str(cfg.node_niceness)] + argv
    return argv


def launch_validator(
    cfg: config.TestnetConfig, index: int, snapshotless: bool = False
) -> int:
    """Start validator ``index`` with existing configs; return PID."""
    name = "validator%d" % index
    workdir = os.path.join(cfg.testnet_dir, "node_working_dirs", name)
    os.makedirs(workdir, exist_ok=True)
    pid = proc.start_daemon(
        name,
        os.path.join(cfg.testnet_dir, "node"),
        os.path.join(cfg.log_dir, "%s.log" % name),
        os.path.join(cfg.pid_dir, "%s.pid" % name),
        node_argv(
            cfg,
            cfg.validator_p2p_port(index),
            cfg.validator_rest_port(index),
            index,
            workdir,
            snapshotless=snapshotless,
        ),
    )
    time.sleep(0.5)
    return pid


def seednode_argv(cfg: config.TestnetConfig) -> List[str]:
    if cfg.node_niceness is not None:
        return ["nice", "-n", str(cfg.node_niceness), "./seednode"]
    return ["./seednode"]


def launch_seednode(cfg: config.TestnetConfig) -> int:
    return proc.start_daemon(
        "seednode",
        os.path.join(cfg.testnet_dir, "seednode"),
        os.path.join(cfg.log_dir, "seednode.log"),
        os.path.join(cfg.pid_dir, "seednode.pid"),
        seednode_argv(cfg),
    )


def launch_proxy(cfg: config.TestnetConfig) -> int:
    return proc.start_daemon(
        "proxy",
        os.path.join(cfg.testnet_dir, "proxy"),
        os.path.join(cfg.log_dir, "proxy.log"),
        os.path.join(cfg.pid_dir, "proxy.pid"),
        ["./proxy", "--log-level", cfg.loglevel],
    )


def txgen_argv(cfg: config.TestnetConfig) -> List[str]:
    argv = [
        "./txgen",
        "--num-accounts", str(cfg.txgen_num_accounts),
        "--log-level", cfg.loglevel,
    ]
    if cfg.txgen_bulk_enabled:
        argv += [
            "--scheduled-bulk-enabled",
            "--scheduled-bulk-size", str(cfg.txgen_bulk_size),
            "--scheduled-bulk-interval-ms", str(cfg.txgen_bulk_interval_ms),
        ]
    return argv


def launch_txgen(cfg: config.TestnetConfig) -> int:
    return proc.start_daemon(
        "txgen",
        os.path.join(cfg.testnet_dir, "txgen"),
        os.path.join(cfg.log_dir, "txgen.log"),
        os.path.join(cfg.pid_dir, "txgen.pid"),
        txgen_argv(cfg),
    )


def pidfile_validator_indices(cfg: config.TestnetConfig) -> List[int]:
    """Sorted validator indices found as pidfiles (source of truth)."""
    if not os.path.isdir(cfg.pid_dir):
        return []
    indices = set()
    for pidfile in os.listdir(cfg.pid_dir):
        if not pidfile.endswith(".pid"):
            continue
        index = proc.validator_index_from_name(pidfile[: -len(".pid")])
        if index is not None:
            indices.add(index)
    return sorted(indices)


def slot_by_index(cfg: config.TestnetConfig) -> Dict[int, Tuple[str, int]]:
    """Map validator index -> (kind, shard) for the configured slots."""
    return {index: (kind, shard) for index, kind, shard in cfg.validator_slots()}


def validator_names(cfg: config.TestnetConfig) -> List[str]:
    """Restartable validator names: cfg slots plus pidfile extras."""
    cfg_indices = {index for index, _kind, _shard in cfg.validator_slots()}
    return [
        "validator%d" % index
        for index in sorted(cfg_indices | set(pidfile_validator_indices(cfg)))
    ]
