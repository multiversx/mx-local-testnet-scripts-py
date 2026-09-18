#!/usr/bin/env python3
"""Restart a single testnet node (validator) without touching the chain.

Stops one process gracefully (SIGTERM first, SIGKILL only on linger,
see ``stop.py``) and starts it again
with the existing configs and working dir, so the chain keeps running.
Configs are never regenerated here (unlike ``start.py``).

Usage:
    make restart                    # interactive: pick from the node list
    make restart NODE=validator2    # non-interactive
    make restart NODE=validator2 SNAPSHOTLESS=1  # snapshotless pruning
    make restart NODE=validator2 CLEAN_DB=1      # wipe validator DB, fresh sync
    python3 src/restart.py --node validator2
    python3 src/restart.py --node validator2 --snapshotless
    python3 src/restart.py --node validator2 --clean-db
    python3 src/restart.py --node seednode   # also accepts seednode/proxy/txgen
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import time
from typing import List, Optional, Tuple

import config
import proc
import services
import stop

LOG = logging.getLogger("restart")


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Restart a single testnet node.")
    parser.add_argument("--node", default=None, metavar="NAME",
                        help="node to restart (e.g. validator2, seednode, "
                             "proxy, txgen). Omit for an interactive list.")
    parser.add_argument("--validators", default=None,
                        help="validators per shard AND on metachain")
    parser.add_argument("--shard-validators", default=None)
    parser.add_argument("--meta-validators", default=None)
    parser.add_argument("--shards", default=None)
    parser.add_argument("--snapshotless", action="store_true",
                        help="restart the node snapshotless (disable state "
                             "snapshots, prune old epochs; less disk use)")
    parser.add_argument("--clean-db", action="store_true",
                        help="delete the validator's db folder while stopped "
                             "(<testnet>/node_working_dirs/<node>/db), so it "
                             "starts fresh and resyncs from the network")
    parser.add_argument("--testnet-dir", default=None)
    parser.add_argument("--mx-chain-go-dir", default=None,
                        help="mx-chain-go checkout to drive "
                             "(default: auto-detect sibling checkouts)")
    return parser.parse_args(argv)


def _pidfile_validator_indices(cfg: config.TestnetConfig) -> List[int]:
    # Backward-compat alias: canonical implementation lives in services.
    return services.pidfile_validator_indices(cfg)


def validator_names(cfg: config.TestnetConfig) -> List[str]:
    # Thin wrapper so existing callers keep working; canonical
    # implementation lives in services (cfg slots plus pidfile extras).
    return services.validator_names(cfg)


def _slot_by_index(cfg: config.TestnetConfig):
    # Backward-compat alias: canonical implementation lives in services.
    return services.slot_by_index(cfg)


def describe_validator(cfg: config.TestnetConfig, index: int, kind: str, shard: int) -> str:
    """Human-readable label for the interactive list."""
    if kind == "meta":
        role = "meta"
    elif kind == "shard":
        role = "shard %d" % shard
    else:
        role = "extra"
    return "validator%d (%s, p2p %d, rest %d)" % (
        index, role, cfg.validator_p2p_port(index), cfg.validator_rest_port(index))


def list_lines(cfg: config.TestnetConfig) -> List[Tuple[str, str]]:
    """Return (name, display line without the list number) for every validator."""
    slots = _slot_by_index(cfg)
    lines = []
    for name in validator_names(cfg):
        index = int(name[len("validator"):])
        kind, shard = slots.get(index, ("extra", -1))
        pidfile = os.path.join(cfg.pid_dir, "%s.pid" % name)
        pid = proc.read_pidfile(pidfile)
        if pid is not None and proc.is_running(pid):
            state = "RUNNING (pid %d)" % pid
        else:
            state = "STOPPED"
        lines.append((name, "%-42s %s" % (describe_validator(cfg, index, kind, shard), state)))
    return lines


def resolve_selection(raw: str, names: List[str]) -> Optional[str]:
    """Resolve interactive/CLI input to a process name, or None if invalid.

    Accepts a 1-based list number ("2"), a validator name ("validator2",
    case-insensitive) or any other known service name passed via --node
    ("seednode", "proxy", "txgen"). Bare validator indices ("2" meaning
    validator2) are NOT accepted to avoid confusion with list numbers.
    """
    cleaned = raw.strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    for name in names:
        if lowered == name.lower():
            return name
    if cleaned.isdigit():
        pos = int(cleaned)
        if 1 <= pos <= len(names):
            return names[pos - 1]
        return None
    for extra in ("seednode", "proxy", "txgen"):
        if lowered == extra:
            return extra
    return None


def validate_target(cfg: config.TestnetConfig, name: str) -> str:
    """Normalize ``name`` (number or service) and check it against ``cfg``.

    Accepts every name in ``validator_names()`` — configured slots plus
    pidfile-discovered extras — so ``--node validator11`` works even
    when ``cfg`` alone describes a smaller topology.
    """
    names = validator_names(cfg)
    resolved = resolve_selection(name, names)
    if resolved is None:
        raise proc.DaemonError(
            "unknown node %r (validators: %s; also accepts seednode/proxy/txgen)."
            % (name, ", ".join(names)))
    index = proc.validator_index_from_name(resolved)
    # Fail fast with a clear message instead of a cryptic daemon error.
    if index is not None:
        missing = []
        if not os.path.isfile(cfg.node_bin):
            missing.append(cfg.node_bin)
        if not os.path.isfile(os.path.join(cfg.testnet_dir, "node", "config", "config_validator.toml")):
            missing.append(os.path.join(cfg.testnet_dir, "node", "config", "config_validator.toml"))
        if missing:
            raise proc.DaemonError(
                "cannot restart %s, missing %s. Run `make start` first."
                % (resolved, ", ".join(missing)))
    return resolved


def validator_workdir(cfg: config.TestnetConfig, name: str) -> str:
    """Return the working dir for a validator (mirrors services.launch_validator)."""
    return os.path.join(cfg.testnet_dir, "node_working_dirs", name)


def validator_db_dir(cfg: config.TestnetConfig, name: str) -> str:
    """Return the DB folder for a validator (``<workdir>/db``).

    The node stores its chain data under ``<working-directory>/db/<chainID>``
    (see ``storage.DefaultDBPath = "db"`` in mx-chain-go), so wiping this
    folder forces a fresh sync on the next start.
    """
    return os.path.join(validator_workdir(cfg, name), "db")


def clean_validator_db(cfg: config.TestnetConfig, name: str) -> bool:
    """Delete the validator's db folder; return True when something was removed.

    Only valid for ``validator<N>`` names (mirrors the ``--snapshotless``
    restriction). Missing folders are not an error — returns False.
    Must be called while the node is stopped.
    """
    if proc.validator_index_from_name(name) is None:
        raise proc.DaemonError(
            "--clean-db only applies to validators, not %r." % name)
    db_dir = validator_db_dir(cfg, name)
    if not os.path.lexists(db_dir):
        LOG.info("[%s] no db folder to clean (%s).", name, db_dir)
        return False
    LOG.info("[%s] deleting db folder %s...", name, db_dir)
    shutil.rmtree(db_dir, ignore_errors=False)
    return True


def _wait_for_exit(pid: Optional[int], timeout: float = 10.0) -> None:
    if pid is None:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not proc.is_running(pid):
            break
        time.sleep(0.2)
    # Give the kernel a moment to release the port before rebinding.
    time.sleep(1.0)


def _start_one(cfg: config.TestnetConfig, name: str,
               snapshotless: bool = False) -> int:
    """Start a single process with existing configs (no regeneration)."""
    if proc.validator_index_from_name(name) is not None:
        index = int(name[len("validator"):])
        services.launch_validator(cfg, index, snapshotless=snapshotless)
        pid = proc.read_pidfile(os.path.join(cfg.pid_dir, "%s.pid" % name))
        if pid is None:
            raise proc.DaemonError("restart of %s failed (no pidfile)." % name)
        return pid
    if snapshotless:
        raise proc.DaemonError(
            "--snapshotless only applies to validators, not %r." % name)
    if name == "seednode":
        return services.launch_seednode(cfg)
    if name == "proxy":
        return services.launch_proxy(cfg)
    if name == "txgen":
        if not os.path.isfile(cfg.txgen_bin):
            raise proc.DaemonError(
                "cannot restart txgen, missing %s. Run `make tx-gen` first." % cfg.txgen_bin)
        return services.launch_txgen(cfg)
    raise proc.DaemonError("unknown process: %r" % name)


def restart_one(cfg: config.TestnetConfig, name: str,
                snapshotless: bool = False, clean_db: bool = False) -> int:
    """Stop ``name`` gracefully and start it again; return the new PID.

    The stop is graceful (SIGTERM first, SIGKILL only if the process
    lingers) so the node can flush storage and shut down cleanly —
    unlike ``make stop`` which SIGKILLs straight away for speed.
    With ``snapshotless=True`` the validator is started with
    ``--operation-mode snapshotless-observer``; ``config.toml`` files
    are never modified.
    With ``clean_db=True`` the validator's db folder
    (``<testnet>/node_working_dirs/<node>/db``) is deleted while the
    node is stopped, so it resyncs from scratch on start. Only valid
    for validators.
    """
    pidfile = os.path.join(cfg.pid_dir, "%s.pid" % name)
    old_pid = proc.read_pidfile(pidfile)
    if clean_db and proc.validator_index_from_name(name) is None:
        raise proc.DaemonError(
            "--clean-db only applies to validators, not %r." % name)
    LOG.info("[%s] stopping (graceful)...", name)
    stop.stop_one(cfg, name, graceful=True)
    _wait_for_exit(old_pid)
    if clean_db and old_pid is not None and proc.is_running(old_pid):
        raise proc.DaemonError(
            "cannot clean db for %s: old process (pid %d) still running."
            % (name, old_pid))
    if clean_db:
        clean_validator_db(cfg, name)
    if snapshotless:
        LOG.info("[%s] starting snapshotless (--operation-mode snapshotless-observer)...",
                 name)
    else:
        LOG.info("[%s] starting...", name)
    return _start_one(cfg, name, snapshotless=snapshotless)


def prompt_selection(cfg: config.TestnetConfig) -> str:
    """Print the validator list and ask which one to restart."""
    rows = list_lines(cfg)
    print("Local testnet nodes (%s):" % cfg.testnet_dir)
    for pos, (_name, line) in enumerate(rows, start=1):
        print("  %2d) %s" % (pos, line))
    names = [name for name, _line in rows]
    while True:
        try:
            raw = input("Select node to restart [1-%d] (or name, Ctrl-C to abort): " % len(names))
        except (EOFError, KeyboardInterrupt):
            print("")
            raise proc.DaemonError("aborted (no node selected).")
        resolved = resolve_selection(raw, names)
        if resolved is not None and proc.validator_index_from_name(resolved) is not None:
            return resolved
        print("invalid selection: %r (enter 1-%d or a validator name)." % (raw.strip(), len(names)))


def prompt_clean_db() -> bool:
    """Ask whether the validator's db folder should be wiped before restart."""
    try:
        raw = input("Delete db folder before restart? [y/N]: ")
    except (EOFError, KeyboardInterrupt):
        print("")
        raise proc.DaemonError("aborted (no node selected).")
    return raw.strip().lower() in ("y", "yes")


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        cfg = config.load_config({
            "validators": args.validators,
            "shards": args.shards,
            "shard_validators": args.shard_validators,
            "meta_validators": args.meta_validators,
            "testnet_dir": args.testnet_dir,
            "mx_chain_go_dir": args.mx_chain_go_dir,
        })
    except config.ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    services.setup_launcher_log(cfg)

    try:
        if args.node:
            name = validate_target(cfg, args.node)
            clean_db = args.clean_db
        else:
            if not validator_names(cfg):
                raise proc.DaemonError("no validators in this topology.")
            name = validate_target(cfg, prompt_selection(cfg))
            # Interactive: --clean-db flag wins, otherwise ask explicitly so
            # the destructive option is discoverable but defaults to safe.
            clean_db = args.clean_db or prompt_clean_db()
        pid = restart_one(cfg, name, snapshotless=args.snapshotless,
                          clean_db=clean_db)
    except (proc.DaemonError, config.ConfigError, OSError) as exc:
        LOG.error("%s", exc)
        print("error: %s" % exc, file=sys.stderr)
        return 1

    print("%s restarted (pid %d)." % (name, pid))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    sys.exit(main())
