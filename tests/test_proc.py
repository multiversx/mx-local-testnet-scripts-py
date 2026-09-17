#!/usr/bin/env python3
"""Tests for proc.py: pidfiles, liveness, daemon start/stop."""

import os
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import proc


class NameTest(unittest.TestCase):
    def test_validator_index(self):
        self.assertEqual(proc.validator_index_from_name("validator0"), 0)
        self.assertEqual(proc.validator_index_from_name("validator12"), 12)
        self.assertIsNone(proc.validator_index_from_name("seednode"))
        self.assertIsNone(proc.validator_index_from_name("proxy"))
        self.assertIsNone(proc.validator_index_from_name("validatorX"))


class PidfileTest(unittest.TestCase):
    def test_missing(self):
        self.assertIsNone(proc.read_pidfile("/nonexistent/x.pid"))

    def test_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "a.pid")
            with open(path, "w") as handle:
                handle.write("not-a-pid\n")
            self.assertIsNone(proc.read_pidfile(path))

    def test_is_running_self(self):
        self.assertTrue(proc.is_running(os.getpid()))

    def test_dead_pid(self):
        child = subprocess.Popen(["true"])
        child.wait()
        self.assertFalse(proc.is_running(child.pid))

    def test_stop_missing_pidfile_ok(self):
        proc.stop_by_pidfile("ghost", "/nonexistent/ghost.pid")

    def test_stop_stale_pidfile_removed(self):
        child = subprocess.Popen(["true"])
        child.wait()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stale.pid")
            with open(path, "w") as handle:
                handle.write("%d\n" % child.pid)
            proc.stop_by_pidfile("stale", path)
            self.assertFalse(os.path.exists(path))


class DaemonTest(unittest.TestCase):
    def test_start_and_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "d.log")
            pidfile = os.path.join(tmp, "d.pid")
            pid = proc.start_daemon(
                "sleeper", tmp, log, pidfile,
                [sys.executable, "-c", "import time; time.sleep(30)"],
            )
            try:
                self.assertTrue(proc.is_running(pid))
                self.assertEqual(proc.read_pidfile(pidfile), pid)
                # Starting again is idempotent: same PID, no new process.
                again = proc.start_daemon(
                    "sleeper", tmp, log, pidfile,
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                )
                self.assertEqual(again, pid)
            finally:
                proc.stop_by_pidfile("sleeper", pidfile)
            self.assertFalse(os.path.exists(pidfile))
            self.assertFalse(proc.is_running(pid))

    def test_failed_start_raises_with_log_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = os.path.join(tmp, "bad.log")
            pidfile = os.path.join(tmp, "bad.pid")
            with self.assertRaises(proc.DaemonError) as ctx:
                proc.start_daemon(
                    "bad", tmp, log, pidfile,
                    [sys.executable, "-c", "import sys; print('boom'); sys.exit(3)"],
                )
            self.assertIn("boom", str(ctx.exception))


class PortTest(unittest.TestCase):
    def test_free_port_empty(self):
        # Port 9 (discard) is privileged and virtually never bound locally.
        self.assertEqual(proc.pids_on_port(9), [])

    def test_listen_only_excludes_clients(self):
        # Regression test for `make restart` killing extra nodes:
        # pids_on_port must return only the LISTEN holder, not every
        # peer with an ESTABLISHED connection to it.
        import shutil
        import socket

        if shutil.which("lsof") is None:
            self.skipTest("lsof not available")
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        port = server.getsockname()[1]
        client = subprocess.Popen(
            [sys.executable, "-c",
             "import socket,time;s=socket.socket();"
             "s.connect(('127.0.0.1',%d));time.sleep(10)" % port]
        )
        try:
            time.sleep(1.0)
            try:
                server.setblocking(False)
                try:
                    conn, _ = server.accept()
                except BlockingIOError:
                    conn = None
            finally:
                server.setblocking(True)
            try:
                pids = proc.pids_on_port(port)
            finally:
                if conn is not None:
                    conn.close()
            self.assertIn(os.getpid(), pids)
            self.assertNotIn(client.pid, pids)
        finally:
            client.terminate()
            client.wait()
            server.close()

    def test_pids_by_port_maps_listener(self):
        import shutil
        import socket

        if shutil.which("lsof") is None:
            self.skipTest("lsof not available")
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(5)
        try:
            port = server.getsockname()[1]
            by_port = proc.pids_by_port()
            self.assertIn(os.getpid(), by_port.get(port, []))
        finally:
            server.close()

    def test_pids_by_port_empty_free_port(self):
        import shutil

        if shutil.which("lsof") is None:
            self.skipTest("lsof not available")
        by_port = proc.pids_by_port()
        self.assertNotIn(9, by_port)


class PreflightTest(unittest.TestCase):
    def _cfg_on_port(self, tmp, port):
        import config as config_mod
        import dataclasses

        cfg = config_mod.load_config({"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
        return dataclasses.replace(
            cfg,
            seednode_port=port,
            proxy_port=port,
            validator_port_origin=port,
            validator_rest_origin=port,
            shard_count=1,
            shard_validator_count=1,
            meta_validator_count=1,
        )

    def test_occupied_port_fails(self):
        import socket
        import start as start_mod

        with tempfile.TemporaryDirectory() as tmp:
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            try:
                cfg = self._cfg_on_port(tmp, server.getsockname()[1])
                with self.assertRaises(proc.DaemonError):
                    start_mod.assert_ports_free(cfg)
            finally:
                server.close()

    def test_own_pid_excluded(self):
        import socket
        import start as start_mod

        with tempfile.TemporaryDirectory() as tmp:
            server = socket.socket()
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            try:
                cfg = self._cfg_on_port(tmp, server.getsockname()[1])
                os.makedirs(cfg.pid_dir, exist_ok=True)
                with open(os.path.join(cfg.pid_dir, "seednode.pid"), "w") as handle:
                    handle.write("%d\n" % os.getpid())
                start_mod.assert_ports_free(cfg)  # must not raise
            finally:
                server.close()


class StatusTest(unittest.TestCase):
    def test_pidfile_driven_listing(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        import status as status_mod
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            pid_dir = os.path.join(tmp, "pids")
            os.makedirs(pid_dir)
            with open(os.path.join(pid_dir, "validator3.pid"), "w") as handle:
                handle.write("%d\n" % os.getpid())
            with open(os.path.join(pid_dir, "proxy.pid"), "w") as handle:
                handle.write("99999999\n")
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            names = [row[0] for row in status_mod.collect_status(cfg)]
            self.assertEqual(names, ["proxy", "validator3"])
            states = {row[0]: row[1] for row in status_mod.collect_status(cfg)}
            self.assertTrue(states["validator3"])
            self.assertFalse(states["proxy"])


class StopOneTest(unittest.TestCase):
    def test_stop_txgen_kills_daemon_and_pidfile(self):
        import dataclasses
        import stop as stop_mod
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            pid_dir = os.path.join(tmp, "pids")
            os.makedirs(pid_dir)
            pid = proc.start_daemon(
                "sleeper", tmp, os.path.join(tmp, "s.log"),
                os.path.join(pid_dir, "txgen.pid"),
                [sys.executable, "-c", "import time; time.sleep(30)"],
            )
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            # Sweep an idle port so the test can't disturb anything real.
            cfg = dataclasses.replace(cfg, txgen_port=47951)
            stop_mod.stop_one(cfg, "txgen")
            self.assertFalse(os.path.exists(os.path.join(pid_dir, "txgen.pid")))
            # SIGKILL is async and the child may linger as a zombie until
            # reaped; wait for it to disappear.
            deadline = time.time() + 5.0
            while time.time() < deadline:
                try:
                    waited, _status = os.waitpid(pid, os.WNOHANG)
                    if waited == pid:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.05)
            self.assertFalse(proc.is_running(pid))

    def test_stop_one_missing_pidfile_ok(self):
        import stop as stop_mod
        import config as config_mod
        import dataclasses

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            cfg = dataclasses.replace(cfg, txgen_port=47951)
            stop_mod.stop_one(cfg, "txgen")  # must not raise

    def test_stop_one_unknown_name_raises(self):
        import stop as stop_mod
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            with self.assertRaises(proc.DaemonError):
                stop_mod.stop_one(cfg, "nope")

    def test_stop_one_default_is_sigkill(self):
        from unittest import mock
        import stop as stop_mod
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            with mock.patch("proc.kill_by_pidfile") as kbf, \
                    mock.patch("proc.kill_by_port"), \
                    mock.patch("proc.stop_by_pidfile") as sbf, \
                    mock.patch("proc.stop_by_port"):
                stop_mod.stop_one(cfg, "txgen")
                self.assertTrue(kbf.called)
                self.assertFalse(sbf.called)

    def test_stop_one_graceful_is_sigterm_first(self):
        from unittest import mock
        import stop as stop_mod
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            with mock.patch("proc.kill_by_pidfile") as kbf, \
                    mock.patch("proc.kill_by_port"), \
                    mock.patch("proc.stop_by_pidfile") as sbf, \
                    mock.patch("proc.stop_by_port") as sbp, \
                    mock.patch("proc.pids_on_port", return_value=[]):
                stop_mod.stop_one(cfg, "txgen", graceful=True, timeout=0.1)
                self.assertFalse(kbf.called)
                self.assertTrue(sbf.called)
                self.assertTrue(sbp.called)

    def test_restart_uses_graceful_stop(self):
        import inspect
        import restart as restart_mod

        self.assertIn("graceful=True", inspect.getsource(restart_mod.restart_one))


if __name__ == "__main__":
    unittest.main()
