#!/usr/bin/env python3
"""Report whether the testnet processes are running.

Exit code is 0 when every expected process is up, 1 otherwise.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import List, Optional, Tuple

import config
import proc

LOG = logging.getLogger("status")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Show testnet process status.")
    parser.add_argument("--testnet-dir", default=None)
    parser.add_argument("--mx-chain-go-dir", default=None,
                        help="mx-chain-go checkout to drive "
                             "(default: auto-detect sibling checkouts)")
    return parser.parse_args(argv)


def expected_processes(cfg: config.TestnetConfig) -> List[Tuple[str, str, str]]:
    """Return (name, pidfile, logfile) for every recorded process.

    The pidfiles are the source of truth, so status stays correct for any
    topology (no need to repeat the start-time flags).
    """
    procs = []
    if not os.path.isdir(cfg.pid_dir):
        return procs
    for pidfile in sorted(os.listdir(cfg.pid_dir)):
        if not pidfile.endswith(".pid"):
            continue
        name = pidfile[: -len(".pid")]
        logfile = os.path.join(cfg.log_dir, "%s.log" % name)
        procs.append((name, os.path.join(cfg.pid_dir, pidfile), logfile))
    return procs


def collect_status(cfg: config.TestnetConfig) -> List[Tuple[str, bool, Optional[int]]]:
    """Return (name, running, pid) rows for every expected process."""
    rows = []
    for name, pidfile, _logfile in expected_processes(cfg):
        pid = proc.read_pidfile(pidfile)
        rows.append((name, pid is not None and proc.is_running(pid), pid))
    return rows


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

    print("testnet dir: %s" % cfg.testnet_dir)
    rows = collect_status(cfg)
    if not rows:
        print("no pidfiles found — testnet is not running.")
        return 1
    all_up = True
    for name, running, pid in rows:
        state = "RUNNING (pid %d)" % pid if running else "STOPPED"
        if not running:
            all_up = False
        print("  %-12s %s" % (name, state))
    return 0 if all_up else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    sys.exit(main())
