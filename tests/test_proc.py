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


class StatusDetailsTest(unittest.TestCase):
    def _cfg(self, tmp):
        import config as config_mod
        return config_mod.load_config(
            {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})

    def test_describe_validator_roles_and_ports(self):
        import status as status_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            # Default topology from variables.sh is 2 shards; metachain
            # validators take the first indices (see validator_slots).
            meta = status_mod.describe_process(cfg, "validator0")
            self.assertIn("meta", meta)
            self.assertIn(str(cfg.validator_p2p_port(0)), meta)
            self.assertIn(str(cfg.validator_rest_port(0)), meta)
            shard = status_mod.describe_process(
                cfg, "validator%d" % cfg.meta_validator_count)
            self.assertIn("shard 0", shard)
            extra = status_mod.describe_process(cfg, "validator9999")
            self.assertIn("extra", extra)

    def test_describe_services(self):
        import status as status_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            self.assertEqual("", status_mod.describe_process(cfg, "seednode"))
            self.assertEqual("", status_mod.describe_process(cfg, "txgen"))
            self.assertIn(str(cfg.proxy_port),
                          status_mod.describe_process(cfg, "proxy"))
            self.assertEqual("", status_mod.describe_process(cfg, "nope"))

    def test_node_liveness_missing_keys(self):
        import status as status_mod

        self.assertEqual(
            "round=- nonce=- epoch=-", status_mod.node_liveness({}))
        self.assertEqual(
            "round=120 nonce=42 epoch=3",
            status_mod.node_liveness({"data": {"metrics": {
                "erd_nonce": 42,
                "erd_current_round": 120,
                "erd_epoch_number": 3,
            }}}))

    def test_node_shard_id(self):
        import status as status_mod

        metrics = lambda shard: {"data": {"metrics": {"erd_shard_id": shard}}}
        self.assertEqual(2, status_mod.node_shard_id(metrics(2)))
        self.assertEqual(4294967295,
                         status_mod.node_shard_id(metrics(4294967295)))
        self.assertEqual(1, status_mod.node_shard_id(metrics("1")))
        self.assertIsNone(status_mod.node_shard_id({}))
        self.assertIsNone(status_mod.node_shard_id(metrics(True)))
        self.assertIsNone(status_mod.node_shard_id(metrics("shard-2")))

    def test_describe_prefers_live_shard(self):
        import status as status_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            # validator9999 has no slot in any topology...
            self.assertIn("extra",
                          status_mod.describe_process(cfg, "validator9999"))
            # ...but the node's own report wins when reachable.
            self.assertIn(
                "shard 2",
                status_mod.describe_process(cfg, "validator9999",
                                            live_shard=2))
            self.assertIn(
                "meta",
                status_mod.describe_process(
                    cfg, "validator9999",
                    live_shard=cfg.metashard_id))

    def test_probe_stopped_is_empty(self):
        import dataclasses
        import status as status_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            cfg = dataclasses.replace(cfg, proxy_port=47951)
            self.assertEqual(
                ("", None), status_mod.probe_process(cfg, "proxy", False))

    def test_probe_down_reports_down(self):
        import dataclasses
        import socket
        import status as status_mod

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        closed = probe.getsockname()[1]
        probe.close()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg(tmp)
            cfg = dataclasses.replace(cfg, proxy_port=closed)
            detail, live_shard = status_mod.probe_process(
                cfg, "proxy", True, timeout=1.0)
            self.assertIn("api DOWN", detail)
            self.assertIsNone(live_shard)

    def test_probe_validator_reports_nonce(self):
        import dataclasses
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import status as status_mod

        payload = {"data": {"metrics": {
            "erd_nonce": 7,
            "erd_current_round": 50,
            "erd_epoch_number": 1,
            "erd_shard_id": 2,
        }}}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib handler naming)
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class FastHTTPServer(ThreadingHTTPServer):
            # http.server resolves the bind address via getfqdn(), i.e. a
            # reverse-DNS lookup that stalls for tens of seconds on
            # machines with broken DNS. Skip it; tests only need IP:port.
            def server_bind(self):
                import socketserver
                socketserver.TCPServer.server_bind(self)
                host, port = self.socket.getsockname()[:2]
                self.server_name = host
                self.server_port = port

        server = FastHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = self._cfg(tmp)
                cfg = dataclasses.replace(
                    cfg,
                    validator_rest_origin=server.server_address[1],
                )
                result = status_mod.probe_process(
                    cfg, "validator0", True, timeout=2.0)
                detail, live_shard = result
                self.assertNotIn("api UP", detail)
                self.assertIn("nonce=7", detail)
                self.assertIn("round=50", detail)
                self.assertIn("epoch=1", detail)
                self.assertEqual(2, live_shard)
        finally:
            server.shutdown()
            thread.join()

    def test_probe_all_probes_concurrently(self):
        import dataclasses
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import socketserver
        import status as status_mod

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (stdlib handler naming)
                body = json.dumps({"data": {"metrics": {
                    "erd_nonce": 9,
                    "erd_current_round": 51,
                    "erd_epoch_number": 2,
                    "erd_shard_id": 1,
                }}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        class FastHTTPServer(ThreadingHTTPServer):
            def server_bind(self):
                socketserver.TCPServer.server_bind(self)
                host, port = self.socket.getsockname()[:2]
                self.server_name = host
                self.server_port = port

        server = FastHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg = self._cfg(tmp)
                cfg = dataclasses.replace(
                    cfg,
                    validator_rest_origin=server.server_address[1],
                )
                rows = [("validator0", True, 1234), ("proxy", False, None)]
                probes = status_mod.probe_all(cfg, rows, timeout=2.0)
                detail, live_shard = probes["validator0"]
                self.assertIn("round=51 nonce=9 epoch=2", detail)
                self.assertEqual(1, live_shard)
                self.assertEqual(("", None), probes["proxy"])
                self.assertEqual({}, status_mod.probe_all(cfg, []))
        finally:
            server.shutdown()
            thread.join()

    def test_main_labels_from_live_shard(self):
        # End-to-end through main(): validator9 is "extra" by default
        # flags, but the probe reports shard 2, so that is displayed.
        from unittest import mock
        import io
        import status as status_mod

        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "pids"))
            with open(os.path.join(tmp, "pids", "validator9.pid"),
                      "w") as handle:
                handle.write("%d\n" % os.getpid())
            canned = {"validator9": ("round=5 nonce=9 epoch=0", 2)}
            with mock.patch.object(status_mod, "probe_all",
                                   return_value=canned):
                with mock.patch("sys.stdout",
                                new_callable=io.StringIO) as out:
                    rc = status_mod.main(["--testnet-dir", tmp])
            self.assertEqual(0, rc)
            self.assertIn("shard 2", out.getvalue())
            self.assertNotIn("extra", out.getvalue())


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
