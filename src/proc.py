#!/usr/bin/env python3
"""Daemon lifecycle helpers: start/stop/status of testnet processes.

Each process runs detached (own session, like ``nohup ... &``) with its
stdout/stderr redirected to a log file and its PID recorded in a pidfile.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from typing import Dict, List, Optional

LOG = logging.getLogger("proc")


class DaemonError(Exception):
    """Raised when a daemon cannot be started or stopped."""


def is_running(pid: int) -> bool:
    """Return True if a process with this PID exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OverflowError:
        return False
    return True


def read_pidfile(pidfile: str) -> Optional[int]:
    """Return the PID stored in a pidfile, or None if missing/invalid."""
    try:
        with open(pidfile, "r", encoding="utf-8") as handle:
            return int(handle.read().strip().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def validator_index_from_name(name: str) -> Optional[int]:
    """Extract the validator index from a ``validator<N>`` process name."""
    match = re.fullmatch(r"validator(\d+)", name)
    return int(match.group(1)) if match else None


def tail_file(path: str, lines: int = 20) -> str:
    """Return the last ``lines`` lines of a file (best effort)."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return "".join(handle.readlines()[-lines:])
    except OSError:
        return ""


def run(cmd: List[str], cwd: str) -> None:
    """Run a foreground command, raising DaemonError with context on failure."""
    LOG.info("running: (cd %s && %s)", cwd, " ".join(cmd))
    try:
        subprocess.run(cmd, cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise DaemonError("command not found: %s (cwd=%s)" % (cmd[0], cwd)) from exc
    except subprocess.CalledProcessError as exc:
        raise DaemonError(
            "command failed with exit %d: %s (cwd=%s)"
            % (exc.returncode, " ".join(cmd), cwd)
        ) from exc


def start_daemon(
    name: str,
    workdir: str,
    logfile: str,
    pidfile: str,
    argv: List[str],
) -> int:
    """Start ``argv`` detached; record PID; verify it stays alive.

    Returns the PID. If a live PID is already recorded, the existing
    process is kept and its PID returned (idempotent start).
    """
    for parent in (workdir, os.path.dirname(logfile), os.path.dirname(pidfile)):
        os.makedirs(parent, exist_ok=True)

    existing = read_pidfile(pidfile)
    if existing is not None and is_running(existing):
        LOG.info("[%s] already running (pid %d), skipping.", name, existing)
        return existing

    LOG.info("[%s] starting, log: %s", name, logfile)
    with open(logfile, "a", encoding="utf-8") as log_handle:
        proc = subprocess.Popen(
            argv,
            cwd=workdir,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    time.sleep(0.5)
    # poll() (not kill(pid, 0): a fast-exiting child would still be an
    # unreaped zombie at this point and look alive).
    if proc.poll() is not None:
        tail = tail_file(logfile)
        raise DaemonError("[%s] FAILED to start, tail of %s:\n%s" % (name, logfile, tail))
    with open(pidfile, "w", encoding="utf-8") as handle:
        handle.write("%d\n" % proc.pid)
    LOG.info("[%s] pid %d", name, proc.pid)
    return proc.pid


def _reaped(pid: int) -> bool:
    """Reap ``pid`` if it is our exited child; True when reaped."""
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
        return waited == pid
    except (ChildProcessError, OSError):
        return False


def stop_by_pidfile(name: str, pidfile: str, timeout: float = 10.0) -> None:
    """SIGTERM a pidfile-recorded process, SIGKILL if it lingers."""
    pid = read_pidfile(pidfile)
    if pid is None:
        return
    if is_running(pid):
        LOG.info("[%s] stopping pid %d...", name, pid)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.time() + timeout
        while time.time() < deadline:
            if _reaped(pid) or not is_running(pid):
                break
            time.sleep(0.5)
        if not _reaped(pid) and is_running(pid):
            LOG.warning("[%s] still alive, killing -9...", name)
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            _reaped(pid)
    try:
        os.remove(pidfile)
    except OSError:
        pass


def kill_by_pidfile(name: str, pidfile: str) -> None:
    """SIGKILL a pidfile-recorded process immediately, without waiting.

    The node processes do not terminate promptly on SIGTERM (graceful
    shutdown takes longer than the stop timeout), so ``make stop`` sends
    SIGKILL straight away instead of waiting out ``stop_by_pidfile``.
    """
    pid = read_pidfile(pidfile)
    if pid is None:
        return
    if is_running(pid):
        LOG.info("[%s] killing pid %d...", name, pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        _reaped(pid)
    try:
        os.remove(pidfile)
    except OSError:
        pass


def pids_on_port(port: int) -> List[int]:
    """Return PIDs listening on a TCP port (via ``lsof``).

    Only ``LISTEN`` sockets are reported. Plain ``lsof -t -i:PORT``
    also matches every *client* connected to the port (ESTABLISHED),
    so a ``kill_by_port`` sweep on one validator's p2p port would
    SIGKILL all its peers — exactly the ``make restart`` bug where
    restarting one node stopped several others.
    """
    try:
        completed = subprocess.run(
            ["lsof", "-t", "-iTCP:%d" % port, "-sTCP:LISTEN"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        LOG.warning("lsof not found, cannot sweep port %d", port)
        return []
    pids = []
    for token in completed.stdout.split():
        try:
            pids.append(int(token))
        except ValueError:
            continue
    return pids


def _int_token(token: str) -> Optional[int]:
    try:
        return int(token)
    except ValueError:
        return None


def _port_from_lsof_name(name: str) -> Optional[int]:
    """Extract the port from an ``lsof -F`` network name.

    Accepts ``*:21500``, ``127.0.0.1:9500`` and bracketed IPv6 forms
    (``[::1]:21500``); the port is always the final ``:PORT`` segment.
    """
    return _int_token(name.rsplit(":", 1)[-1])


def pids_by_port() -> Dict[int, List[int]]:
    """Return ``{port: [listening pids]}`` from a single ``lsof`` sweep.

    This is the batched counterpart of ``pids_on_port``: one ``lsof``
    call reports every LISTEN socket on the machine, so callers checking
    many ports (e.g. the start preflight over all validator p2p/REST
    ports) avoid spawning one subprocess per port. Only ``LISTEN``
    sockets are reported, never connected clients.
    """
    try:
        completed = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pn"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        LOG.warning("lsof not found, cannot sweep ports")
        return {}
    ports: Dict[int, List[int]] = {}
    current: Optional[int] = None
    for line in completed.stdout.splitlines():
        if line.startswith("p"):
            current = _int_token(line[1:])
        elif line.startswith("n") and current is not None:
            port = _port_from_lsof_name(line[1:])
            if port is not None:
                ports.setdefault(port, []).append(current)
    return ports


def stop_by_port(port: int) -> None:
    """SIGTERM whatever listens on a TCP port (fallback sweep)."""
    for pid in pids_on_port(port):
        LOG.info("Stopping process %d on port %d", pid, port)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue


def kill_by_port(port: int) -> None:
    """SIGKILL whatever listens on a TCP port (fast fallback sweep)."""
    for pid in pids_on_port(port):
        LOG.info("Killing process %d on port %d", pid, port)
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
