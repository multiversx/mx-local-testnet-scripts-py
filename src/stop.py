#!/usr/bin/env python3
"""Stop a local testnet started by ``start.py``.

Kills by pidfile first, then falls back to an ``lsof`` sweep over the known
ports. Safe to run repeatedly: missing pidfiles and free ports are ignored.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from typing import List, Optional

import config
import proc
import services

LOG = logging.getLogger("stop")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stop a local Mac testnet.")
    parser.add_argument("--clean", action="store_true",
                        help="also remove the whole testnet directory")
    parser.add_argument("--only", default=None, metavar="NAME",
                        help="stop a single process only "
                             "(e.g. txgen, proxy, seednode, validator3)")
    parser.add_argument("--testnet-dir", default=None)
    parser.add_argument("--mx-chain-go-dir", default=None,
                        help="mx-chain-go checkout to drive "
                             "(default: auto-detect sibling checkouts)")
    return parser.parse_args(argv)


def service_ports(cfg: config.TestnetConfig, name: str) -> List[int]:
    """Return the sweep ports for a single process name."""
    return services.service_ports(cfg, name)


def service_port(cfg: config.TestnetConfig, name: str) -> int:
    """Return the sweep port for a single process name (compat wrapper)."""
    return service_ports(cfg, name)[0]


def stop_one(cfg: config.TestnetConfig, name: str, graceful: bool = False,
             timeout: float = 10.0) -> None:
    """Stop a single testnet process by pidfile, then sweep its port(s).

    Everything else keeps running. Missing pidfiles and free ports are
    ignored, so this is safe to run repeatedly.

    ``graceful=False`` (default) SIGKILLs straight away — fast, used by
    ``make stop``. ``graceful=True`` SIGTERMs first and only SIGKILLs
    lingerers after ``timeout`` — used by ``make restart`` so the node
    can flush its DB and close gracefully instead of being killed.
    """
    ports = service_ports(cfg, name)
    pidfile = os.path.join(cfg.pid_dir, "%s.pid" % name)
    if not graceful:
        proc.kill_by_pidfile(name, pidfile)
        for port in ports:
            proc.kill_by_port(port)
        return
    proc.stop_by_pidfile(name, pidfile, timeout=timeout)
    # Orphaned listener without a pidfile: ask it to exit gracefully too.
    # stop_by_port is LISTEN-only, so peers connected to a validator are
    # never signalled — only the listener is.
    for port in ports:
        proc.stop_by_port(port)
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(not proc.pids_on_port(port) for port in ports):
            break
        time.sleep(0.2)
    for port in ports:
        proc.kill_by_port(port)


def stop_all(cfg: config.TestnetConfig) -> None:
    """Stop every testnet process described by ``cfg``.

    Sends SIGKILL straight away: the nodes do not terminate promptly on
    SIGTERM, so waiting for graceful shutdown makes ``make stop`` take
    ~10 s per validator.
    """
    if os.path.isdir(cfg.pid_dir):
        for pidfile in sorted(os.listdir(cfg.pid_dir)):
            if not pidfile.endswith(".pid"):
                continue
            name = pidfile[: -len(".pid")]
            path = os.path.join(cfg.pid_dir, pidfile)
            proc.kill_by_pidfile(name, path)
            for port in service_ports(cfg, name):
                proc.kill_by_port(port)

    # Fallback sweep over every known port (covers custom topologies whose
    # pidfiles are already gone).
    proc.kill_by_port(cfg.txgen_port)
    proc.kill_by_port(cfg.proxy_port)
    proc.kill_by_port(cfg.seednode_port)
    for index in range(cfg.validator_count()):
        proc.kill_by_port(cfg.validator_p2p_port(index))
        proc.kill_by_port(cfg.validator_rest_port(index))


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = config.load_config({
            "testnet_dir": args.testnet_dir,
            "mx_chain_go_dir": args.mx_chain_go_dir,
        })
    except config.ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    try:
        if args.only:
            stop_one(cfg, args.only)
        else:
            stop_all(cfg)
    except (proc.DaemonError, OSError) as exc:
        LOG.error("%s", exc)
        print("error: %s" % exc, file=sys.stderr)
        return 1

    if args.clean:
        LOG.info("Removing %s...", cfg.testnet_dir)
        shutil.rmtree(cfg.testnet_dir, ignore_errors=True)

    print("Testnet stopped successfully.")
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    sys.exit(main())
