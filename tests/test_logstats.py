#!/usr/bin/env python3
"""Tests for logstats.py: per-file log level counting."""

import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import logstats


SAMPLE = """\
ERROR [2025-04-29 07:46:37.102] [transactions]  [0/4/805/(END_ROUND)] something broke
WARN [2025-04-29 07:46:38.102] [process]  [metachain/13/2648/(START_ROUND)] slow round
INFO [2025-04-29 07:46:39.102] [node]  [/0/0/] started
a line mentioning ERROR inside the message is OTHER, not an error
"""


class CountLevelsTest(unittest.TestCase):
    def test_counts_levels_and_other(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "validator0.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(SAMPLE)
            counts = logstats.count_levels(path)
        self.assertEqual(counts["ERROR"], 1)
        self.assertEqual(counts["WARN"], 1)
        self.assertEqual(counts["INFO"], 1)
        self.assertEqual(counts["DEBUG"], 0)
        self.assertEqual(counts["TRACE"], 0)
        self.assertEqual(counts["OTHER"], 1)

    def test_counts_colored_node_format(self):
        # Real node output wraps the level in ANSI color codes and pads
        # INFO/WARN with a space; the proxy omits the space entirely.
        body = (
            "\x1b[0;36mDEBUG\x1b[0m[2026-09-17 17:39:09.971] [main]  [/0/0/] x\n"
            "\x1b[0;32mINFO \x1b[0m[2026-09-17 17:39:09.971] [main]  [/0/0/] y\n"
            "\x1b[0;33mWARN \x1b[0m[2026-09-17 17:39:09.971] [main]  [/0/0/] z\n"
            "\x1b[0;31mERROR\x1b[0m[2026-09-17 17:39:09.971] [main]  [/0/0/] w\n"
            "ERROR[2026-09-17 17:39:35.663]   proxy style, no space\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "validator0.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            counts = logstats.count_levels(path)
        self.assertEqual(
            counts, {"ERROR": 2, "WARN": 1, "INFO": 1, "DEBUG": 1,
                     "TRACE": 0, "OTHER": 0})

    def test_counts_launcher_format(self):
        body = ("2026-09-17 17:38:45,144 INFO  [start] building...\n"
                "2026-09-17 17:38:45,144 WARNING  [start] careful\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "launcher.log")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            counts = logstats.count_levels(path)
        self.assertEqual(counts["INFO"], 1)
        self.assertEqual(counts["WARN"], 1)
        self.assertEqual(counts["OTHER"], 0)

    def test_missing_file_is_zero(self):
        with self.assertLogs(level="WARNING"):
            counts = logstats.count_levels("/nonexistent/validator0.log")
        self.assertEqual(counts["ERROR"], 0)
        self.assertEqual(counts["OTHER"], 0)


class CollectCountsTest(unittest.TestCase):
    def test_sorted_and_node_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("validator1.log", "validator0.log"):
                with open(os.path.join(tmp, name), "w",
                          encoding="utf-8") as handle:
                    handle.write("INFO [t] [l]  [c] hi\n")
            rows = logstats.collect_counts(tmp)
            self.assertEqual([n for n, _c in rows],
                             ["validator0.log", "validator1.log"])
            only = logstats.collect_counts(tmp, node="validator1")
            self.assertEqual([n for n, _c in only], ["validator1.log"])
            missing = logstats.collect_counts(tmp, node="validator9")
            self.assertEqual(missing, [])


class RenderTest(unittest.TestCase):
    def test_table_has_total(self):
        rows = [("a.log", {"ERROR": 2, "WARN": 1, "INFO": 0,
                           "DEBUG": 0, "TRACE": 0, "OTHER": 0})]
        out = logstats.render(rows)
        self.assertIn("ERROR", out)
        self.assertIn("a.log", out)
        self.assertIn("TOTAL", out)


class ErrorLinesTest(unittest.TestCase):
    def _write(self, tmp, name, body):
        path = os.path.join(tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_collects_only_real_error_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "validator0.log",
                        "ERROR [t] [l]  [c] boom\n"
                        "a line mentioning ERROR inside is skipped\n"
                        "INFO [t] [l]  [c] fine\n")
            self._write(tmp, "proxy.log", "INFO [t] [l]  [c] fine\n")
            rows = logstats.collect_error_lines(tmp)
        self.assertEqual(len(rows), 1)
        name, lines, hidden = rows[0]
        self.assertEqual(name, "validator0.log")
        self.assertEqual(len(lines), 1)
        self.assertIn("boom", lines[0])
        self.assertEqual(hidden, 0)

    def test_caps_per_file_and_reports_hidden(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "validator0.log",
                        "".join("ERROR [t] [l]  [c] e%d\n" % i
                                for i in range(5)))
            rows = logstats.collect_error_lines(tmp, max_per_file=2)
        _name, lines, hidden = rows[0]
        self.assertEqual(len(lines), 2)
        self.assertEqual(hidden, 3)
        out = logstats.render_errors(rows)
        self.assertIn("and 3 more ERROR lines", out)

    def test_trims_overlong_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "validator0.log",
                        "ERROR [t] [l]  [c] " + "x" * 600 + "\n")
            rows = logstats.collect_error_lines(tmp)
        _name, lines, _hidden = rows[0]
        self.assertTrue(lines[0].endswith("..."))
        self.assertLessEqual(len(lines[0]),
                             logstats.MAX_ERROR_LINE_LEN + 3)

    def test_collects_colored_error_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "validator0.log",
                        "\x1b[0;31mERROR\x1b[0m[t] [l]  [c] colored boom\n"
                        "INFO [t] [l]  [c] fine\n")
            rows = logstats.collect_error_lines(tmp)
        _name, lines, _hidden = rows[0]
        self.assertEqual(len(lines), 1)
        self.assertIn("colored boom", lines[0])
        self.assertNotIn("\x1b", lines[0])

    def test_collects_warn_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._write(tmp, "validator0.log",
                        "WARN [t] [l]  [c] slow round\n"
                        "ERROR [t] [l]  [c] boom\n"
                        "INFO [t] [l]  [c] fine\n")
            rows = logstats.collect_level_lines(tmp, level="WARN")
        self.assertEqual(len(rows), 1)
        _name, lines, hidden = rows[0]
        self.assertEqual(len(lines), 1)
        self.assertIn("slow round", lines[0])
        self.assertEqual(hidden, 0)
        out = logstats.render_errors(rows, level="WARN")
        self.assertIn("WARN lines:", out)
        self.assertNotIn("ERROR lines:", out)


class MainTest(unittest.TestCase):
    def test_no_logs_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("sys.stdout", new_callable=io.StringIO):
                rc = logstats.main(["--testnet-dir", tmp])
        self.assertEqual(1, rc)

    def test_reports_counts(self):
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            os.makedirs(cfg.log_dir, exist_ok=True)
            with open(os.path.join(cfg.log_dir, "proxy.log"), "w",
                      encoding="utf-8") as handle:
                handle.write(SAMPLE)
            with mock.patch("sys.stdout",
                            new_callable=io.StringIO) as out:
                rc = logstats.main(["--testnet-dir", tmp])
        self.assertEqual(0, rc)
        self.assertIn("proxy.log", out.getvalue())
        self.assertIn("ERROR", out.getvalue())

    def test_main_prints_error_lines(self):
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            os.makedirs(cfg.log_dir, exist_ok=True)
            with open(os.path.join(cfg.log_dir, "validator0.log"), "w",
                      encoding="utf-8") as handle:
                handle.write(SAMPLE)
            with mock.patch("sys.stdout",
                            new_callable=io.StringIO) as out:
                rc = logstats.main(["--testnet-dir", tmp])
        self.assertEqual(0, rc)
        body = out.getvalue()
        self.assertIn("ERROR lines:", body)
        self.assertIn("something broke", body)
        self.assertIn("=== validator0.log ===", body)
        # SAMPLE has a WARN line too, shown in its own section.
        self.assertIn("WARN lines:", body)
        self.assertIn("slow round", body)

    def test_main_max_errors_zero_hides_lines(self):
        import config as config_mod

        with tempfile.TemporaryDirectory() as tmp:
            cfg = config_mod.load_config(
                {"testnet_dir": tmp}, env={"TESTNETDIR": tmp})
            os.makedirs(cfg.log_dir, exist_ok=True)
            with open(os.path.join(cfg.log_dir, "validator0.log"), "w",
                      encoding="utf-8") as handle:
                handle.write(SAMPLE)
            with mock.patch("sys.stdout",
                            new_callable=io.StringIO) as out:
                rc = logstats.main(["--testnet-dir", tmp,
                                    "--max-errors", "0",
                                    "--max-warns", "0"])
        self.assertEqual(0, rc)
        self.assertNotIn("something broke", out.getvalue())
        self.assertNotIn("slow round", out.getvalue())


if __name__ == "__main__":
    unittest.main()
