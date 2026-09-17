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
import services

LOG = logging.getLogger("status")

# Metric keys read from a validator's /node/status response
# (``{"data": {"metrics": {...}}}``); looked up defensively so a node
# version that renames one key degrades to "-" instead of crashing.
_METRIC_KEYS = (
    ("round", ("erd_current_round",)),
    ("nonce", ("erd_nonce",)),
    ("epoch", ("erd_epoch_number",)),
)

PROBE_TIMEOUT = 2.0


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


def describe_process(cfg: config.TestnetConfig, name: str,
                     live_shard=None) -> str:
    """Static one-line detail for a process (no network I/O).

    Validators show their role (meta / shard N / extra when the pidfile
    has no slot in this topology, e.g. after downsizing) plus p2p and
    REST ports; proxy shows its listen address; seednode/txgen show no
    extra detail (just RUNNING/STOPPED).
    ``live_shard`` (from the node's own /node/status, when reachable)
    wins over the flag-derived slot, so labels stay right even when
    status runs with different flags than start.
    """
    index = proc.validator_index_from_name(name)
    if index is not None:
        if live_shard is None:
            kind, shard = services.slot_by_index(cfg).get(index, ("extra", -1))
        elif live_shard == cfg.metashard_id:
            kind, shard = "meta", live_shard
        else:
            kind, shard = "shard", live_shard
        if kind == "meta":
            role = "meta"
        elif kind == "shard":
            role = "shard %d" % shard
        else:
            role = "extra"
        return "%s, p2p %d, rest localhost:%d" % (
            role, cfg.validator_p2p_port(index),
            cfg.validator_rest_port(index))
    if name == "proxy":
        return "http://127.0.0.1:%d" % cfg.proxy_port
    return ""


def probe_http_json(url: str, timeout: float = PROBE_TIMEOUT):
    """GET ``url`` and parse the JSON body.

    Return ``(True, payload)`` on HTTP 2xx, ``(False, error)`` otherwise
    (connection refused, timeout, non-2xx, bad JSON). Never raises.
    """
    import json
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if not 200 <= resp.status < 300:
                return False, "http %d" % resp.status
            return True, json.load(resp)
    except Exception as exc:  # failure IS the signal here
        return False, str(exc) or type(exc).__name__


def node_liveness(payload: object) -> str:
    """Render ``round=.. nonce=.. epoch=..`` from a /node/status payload."""
    metrics = {}
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            inner = data.get("metrics")
            if isinstance(inner, dict):
                metrics = inner
    parts = []
    for label, keys in _METRIC_KEYS:
        value = "-"
        for key in keys:
            if metrics.get(key) is not None:
                value = metrics[key]
                break
        parts.append("%s=%s" % (label, value))
    return " ".join(parts)


def node_shard_id(payload: object):
    """Shard id the node reports about itself (``erd_shard_id``), or None."""
    metrics = {}
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            inner = data.get("metrics")
            if isinstance(inner, dict):
                metrics = inner
    value = metrics.get("erd_shard_id")
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def probe_process(cfg: config.TestnetConfig, name: str, running: bool,
                  timeout: float = PROBE_TIMEOUT):
    """Live probe of one process; return ``(detail, live_shard)``.

    ``detail`` is the display string ("" when not running); ``live_shard``
    is the validator's self-reported shard id (None when unknown or not
    a validator). A process can be RUNNING yet unreachable (still
    booting, wedged) — that is exactly what this surfaces.
    """
    if not running:
        return "", None
    index = proc.validator_index_from_name(name)
    if index is not None:
        # 127.0.0.1 (not "localhost"): same endpoint, but immune to
        # slow/broken local DNS when resolving the name.
        ok, payload = probe_http_json(
            "http://127.0.0.1:%d/node/status" % cfg.validator_rest_port(index),
            timeout=timeout,
        )
        if ok:
            return node_liveness(payload), node_shard_id(payload)
        return "api DOWN (%s)" % payload, None
    if name == "proxy":
        ok, payload = probe_http_json(
            "http://127.0.0.1:%d/network/config" % cfg.proxy_port,
            timeout=timeout,
        )
        return ("", None) if ok else ("api DOWN (%s)" % payload, None)
    return "", None


def probe_all(cfg: config.TestnetConfig, rows, timeout: float = PROBE_TIMEOUT):
    """Probe every row concurrently; return ``{name: probe detail}``.

    Probes are independent HTTP/TCP round-trips, so they run in a thread
    pool: worst case is ~one timeout, not one timeout per process.
    Never raises — an unexpected probe failure becomes a detail string.
    """
    import concurrent.futures

    probes = {}
    if not rows:
        return probes
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(len(rows), 32)
    ) as pool:
        future_of = {
            pool.submit(probe_process, cfg, name, running, timeout): name
            for name, running, _pid in rows
        }
        for future in concurrent.futures.as_completed(future_of):
            name = future_of[future]
            try:
                probes[name] = future.result()
            except Exception as exc:  # must not break the report
                probes[name] = ("probe ERROR (%s)" % (exc or type(exc).__name__), None)
    return probes


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
    probes = probe_all(cfg, rows)
    for name, running, pid in rows:
        state = "RUNNING (pid %d)" % pid if running else "STOPPED"
        if not running:
            all_up = False
        probe, live_shard = probes.get(name, ("", None))
        line = "  %-12s %-17s %s" % (
            name, state, describe_process(cfg, name, live_shard=live_shard))
        if probe:
            line += "  %s" % probe
            if "DOWN" in probe or "ERROR" in probe:
                all_up = False
        print(line.rstrip())
    return 0 if all_up else 1


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    sys.exit(main())
