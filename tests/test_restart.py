#!/usr/bin/env python3
"""Tests for restart.py: snapshotless process flag."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import restart


class SnapshotlessFlagTest(unittest.TestCase):
    def test_flag_defaults_off(self):
        self.assertFalse(restart.parse_args([]).snapshotless)

    def test_flag_parses(self):
        args = restart.parse_args(["--node", "validator2", "--snapshotless"])
        self.assertTrue(args.snapshotless)
        self.assertEqual(args.node, "validator2")


class SnapshotlessArgvTest(unittest.TestCase):
    def test_node_argv_without_flag(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        import config as config_mod
        import start as start_mod
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            argv = start_mod._node_argv(cfg, 21500, 9500, 0, tmp)
            self.assertNotIn("--operation-mode", argv)
            self.assertIn("--disable-ansi-color", argv)

    def test_node_argv_with_snapshotless(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        import config as config_mod
        import start as start_mod
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            argv = start_mod._node_argv(
                cfg, 21500, 9500, 0, tmp, snapshotless=True)
            self.assertIn("--operation-mode", argv)
            self.assertIn("snapshotless-observer", argv)

    def test_snapshotless_rejected_for_non_validators(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        import config as config_mod
        import proc
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            with self.assertRaises(proc.DaemonError):
                restart._start_one(cfg, "proxy", snapshotless=True)

    def test_restart_passes_flag_to_launcher(self):
        from unittest import mock

        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
        import config as config_mod
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            with mock.patch("stop.stop_one"), \
                    mock.patch.object(restart, "_wait_for_exit"), \
                    mock.patch("proc.read_pidfile", return_value=99999), \
                    mock.patch("services.launch_validator") as launch:
                restart.restart_one(cfg, "validator0", snapshotless=True)
                launch.assert_called_once_with(cfg, 0, snapshotless=True)


if __name__ == "__main__":
    unittest.main()
