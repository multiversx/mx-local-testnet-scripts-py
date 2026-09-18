#!/usr/bin/env python3
"""Count log levels per testnet log file.

Scans ``<testnet>/logs/*.log`` line by line (log files can be GBs, so
nothing is loaded fully into memory) and prints one row per file with
the number of ``ERROR`` / ``WARN`` / ``INFO`` / ``DEBUG`` / ``TRACE``
lines plus a ``TOTAL`` row. When ``ERROR`` or ``WARN`` lines exist, they
are printed as well (capped per file, longest lines trimmed), so a
failing testnet shows what broke without opening klogg.

Node log lines look like::

    ERROR [2025-04-29 07:46:37.102] [logger]  [shard/epoch/round/...] message

and the proxy omits the space (``ERROR[...]``). Only a leading level
token counts: an ``ERROR`` appearing inside a message body, a Go stack
trace, an ASCII table row or a ``[GIN-debug]`` gin-framework line does
not, and falls into ``OTHER``.

``launcher.log`` (the tool's own orchestration output) is always
skipped.

Exit code is 0 even when errors are found (this is a report, not a
health check); 1 only when there are no log files to scan.
"""

from __future__ import annotations

import argparse
import glob
import logging
import os
import re
import sys
from typing import Dict, List, Optional

import config

LOG = logging.getLogger("logstats")

LEVELS = ("ERROR", "WARN", "INFO", "DEBUG", "TRACE")

_LINE_RE = re.compile(r"^(ERROR|WARN|INFO|DEBUG|TRACE)\s*\[")


def level_of(line: str):
    """Return the log level of one line, or None when it has none.

    Only a leading ``LEVEL [...]`` token counts; the ``launcher.log``
    Python-logging format is not recognised (that file is the tool's
    own output and is skipped by the scanner).
    """
    match = _LINE_RE.match(line)
    if match:
        return match.group(1)
    return None

# Lines shown per file for the ERROR/WARN report sections
# (longest lines trimmed to MAX_ERROR_LINE_LEN).
MAX_ERRORS_PER_FILE = 20
MAX_ERROR_LINE_LEN = 500


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Count log levels per file.")
    parser.add_argument("--node", default=None, metavar="NAME",
                        help="only scan one log file "
                             "(e.g. validator2, proxy, seednode)")
    parser.add_argument("--max-errors", type=int, default=MAX_ERRORS_PER_FILE,
                        metavar="N",
                        help="ERROR lines printed per file "
                             "(default: %(default)s, 0 disables)")
    parser.add_argument("--max-warns", type=int, default=MAX_ERRORS_PER_FILE,
                        metavar="N",
                        help="WARN lines printed per file "
                             "(default: %(default)s, 0 disables)")
    parser.add_argument("--testnet-dir", default=None)
    parser.add_argument("--mx-chain-go-dir", default=None,
                        help="mx-chain-go checkout to drive "
                             "(default: auto-detect sibling checkouts)")
    return parser.parse_args(argv)


def count_levels(path: str) -> Dict[str, int]:
    """Return ``{level: count}`` for one log file (streamed, never raises)."""
    counts: Dict[str, int] = {level: 0 for level in LEVELS}
    counts["OTHER"] = 0
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                level = level_of(line)
                if level is not None:
                    counts[level] += 1
                elif line.strip():
                    counts["OTHER"] += 1
    except OSError as exc:
        LOG.warning("cannot read %s: %s", path, exc)
    return counts


def _resolve_paths(log_dir: str, node: Optional[str] = None) -> List[str]:
    """Return sorted log paths to scan (single-file when ``node`` is set).

    ``launcher.log`` (the tool's own orchestration output) is always
    excluded — it is not useful for diagnosing node issues.
    """
    if node:
        paths = [os.path.join(log_dir, "%s.log" % node)]
        return [p for p in paths if os.path.isfile(p)]
    return sorted(
        p for p in glob.glob(os.path.join(log_dir, "*.log"))
        if os.path.basename(p) != "launcher.log"
    )


def collect_counts(log_dir: str, node: Optional[str] = None):
    """Return ``[(filename, counts)]`` sorted by filename."""
    return [(os.path.basename(p), count_levels(p))
            for p in _resolve_paths(log_dir, node)]


def collect_level_lines(log_dir: str, node: Optional[str] = None,
                        level: str = "ERROR",
                        max_per_file: int = MAX_ERRORS_PER_FILE):
    """Return ``[(filename, lines, hidden)]`` for files with ``level`` lines.

    ``lines`` holds up to ``max_per_file`` raw lines (stripped of the
    trailing newline, overlong lines trimmed); ``hidden`` is the number
    of further lines not shown. Files without such lines are skipped.
    Streamed one pass per file; never raises.
    """
    rows = []
    for path in _resolve_paths(log_dir, node):
        lines: List[str] = []
        hidden = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if level_of(line) != level:
                        continue
                    if len(lines) < max_per_file:
                        lines.append(_trim_line(line))
                    else:
                        hidden += 1
        except OSError as exc:
            LOG.warning("cannot read %s: %s", path, exc)
            continue
        if lines or hidden:
            rows.append((os.path.basename(path), lines, hidden))
    return rows


def collect_error_lines(log_dir: str, node: Optional[str] = None,
                        max_per_file: int = MAX_ERRORS_PER_FILE):
    """Return ``[(filename, lines, hidden)]`` for files containing ERRORs."""
    return collect_level_lines(log_dir, node, level="ERROR",
                               max_per_file=max_per_file)


def _trim_line(line: str, limit: int = MAX_ERROR_LINE_LEN) -> str:
    """Strip one log line, trimming overlong lines with an ellipsis."""
    line = line.rstrip("\n")
    if len(line) > limit:
        return line[:limit] + "..."
    return line


def render(rows) -> str:
    """Render the counts table (per-file rows plus a TOTAL row)."""
    total: Dict[str, int] = {level: 0 for level in LEVELS}
    total["OTHER"] = 0
    for _name, counts in rows:
        for level in list(LEVELS) + ["OTHER"]:
            total[level] += counts.get(level, 0)
    header = "%-18s %7s %7s %7s %7s %7s %7s" % (
        "log file", "ERROR", "WARN", "INFO", "DEBUG", "TRACE", "OTHER")
    lines = [header]
    for name, counts in rows:
        lines.append("%-18s %7d %7d %7d %7d %7d %7d" % (
            name, counts["ERROR"], counts["WARN"], counts["INFO"],
            counts["DEBUG"], counts["TRACE"], counts["OTHER"]))
    lines.append("%-18s %7d %7d %7d %7d %7d %7d" % (
        "TOTAL", total["ERROR"], total["WARN"], total["INFO"],
        total["DEBUG"], total["TRACE"], total["OTHER"]))
    return "\n".join(lines)


def render_errors(rows, level: str = "ERROR") -> str:
    """Render collected ERROR/WARN lines grouped by file."""
    out = ["", "%s lines:" % level]
    for name, lines, hidden in rows:
        out.append("=== %s ===" % name)
        out.extend(lines)
        if hidden:
            out.append("... and %d more %s lines in %s" % (hidden, level, name))
    return "\n".join(out)


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
    rows = collect_counts(cfg.log_dir, node=args.node)
    if not rows:
        if args.node:
            print("no log file for %r under %s." % (args.node, cfg.log_dir))
        else:
            print("no log files under %s yet. Run 'make start' first."
                  % cfg.log_dir)
        return 1
    print(render(rows))
    if args.max_errors > 0:
        error_rows = collect_error_lines(
            cfg.log_dir, node=args.node, max_per_file=args.max_errors)
        if error_rows:
            print(render_errors(error_rows))
    if args.max_warns > 0:
        warn_rows = collect_level_lines(
            cfg.log_dir, node=args.node, level="WARN",
            max_per_file=args.max_warns)
        if warn_rows:
            print(render_errors(warn_rows, level="WARN"))
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
    )
    sys.exit(main())
