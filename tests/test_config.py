#!/usr/bin/env python3
"""Tests for config.py: shell-file parsing, precedence, derivation."""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import config


def cli(**overrides):
    base = {
        "validators": None,
        "shards": None,
        "shard_validators": None,
        "meta_validators": None,
        "proxy_port": None,
        "log_level": None,
        "genesis_delay": None,
        "supernova_round": None,
        "testnet_dir": None,
    }
    base.update(overrides)
    return base


def chain_scripts_dir():
    """scripts/testnet dir of the auto-detected mx-chain-go checkout."""
    return config.chain_scripts_dir(config.resolve_chain_root({}, {}))


def variables_file():
    return os.path.join(chain_scripts_dir(), "variables.sh")


def local_file():
    return os.path.join(chain_scripts_dir(), "local.sh")


class ParseShellExportsTest(unittest.TestCase):
    def test_static_values(self):
        values = config.parse_shell_exports(variables_file())
        # Upstream variables.sh currently defaults to 2 shards.
        self.assertEqual(values["SHARDCOUNT"], "2")
        self.assertEqual(values["PORT_PROXY"], "7950")
        self.assertEqual(values["LOGLEVEL"], "*:INFO")
        self.assertEqual(values["HYSTERESIS"], "0.0")

    def test_computed_values_skipped(self):
        values = config.parse_shell_exports(variables_file())
        self.assertNotIn("MULTIVERSXDIR", values)
        self.assertNotIn("META_CONSENSUS_SIZE", values)
        self.assertNotIn("TOTAL_NODECOUNT", values)

    def test_supernova_round_default(self):
        # variables.sh no longer defines SUPERNOVA_ROUND (removed upstream);
        # the Python default (440, matching config.toml/enableRounds.toml)
        # applies via load_config.
        values = config.parse_shell_exports(variables_file())
        self.assertEqual(values.get("SUPERNOVA_ROUND", "440"), "440")
        cfg = config.load_config(cli(), env={})
        self.assertEqual(cfg.supernova_round, 440)

    def test_local_overrides_win(self):
        if not os.path.isfile(local_file()):
            self.skipTest("no local.sh present")
        values = config.parse_shell_exports(local_file())
        self.assertTrue(values["CONFIGGENERATORDIR"].endswith("cmd/filegen"))


class ChainRootTest(unittest.TestCase):
    def test_auto_detects_sibling(self):
        root = config.resolve_chain_root({}, {})
        self.assertTrue(os.path.isdir(os.path.join(root, "cmd", "node")))

    def test_explicit_dir_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "cmd", "node"))
            self.assertEqual(
                config.resolve_chain_root({"mx_chain_go_dir": tmp}, {}), tmp)
            self.assertEqual(
                config.resolve_chain_root({}, {"MX_CHAIN_GO_DIR": tmp}), tmp)

    def test_explicit_cli_beats_env(self):
        with tempfile.TemporaryDirectory() as tmp1, \
                tempfile.TemporaryDirectory() as tmp2:
            os.makedirs(os.path.join(tmp1, "cmd", "node"))
            os.makedirs(os.path.join(tmp2, "cmd", "node"))
            root = config.resolve_chain_root(
                {"mx_chain_go_dir": tmp1}, {"MX_CHAIN_GO_DIR": tmp2})
            self.assertEqual(root, tmp1)

    def test_invalid_dir_raises(self):
        with self.assertRaises(config.ConfigError):
            config.resolve_chain_root(
                {"mx_chain_go_dir": "/nonexistent/chain-root"}, {})


class PrecedenceTest(unittest.TestCase):
    def test_defaults(self):
        # Effective defaults come from variables.sh (currently 2 shards).
        cfg = config.load_config(cli(), env={})
        self.assertEqual(cfg.shard_count, 2)
        self.assertEqual(cfg.shard_validator_count, 3)
        self.assertEqual(cfg.meta_validator_count, 3)
        self.assertEqual(cfg.loglevel, "*:DEBUG")
        self.assertTrue(cfg.use_proxy)
        self.assertEqual(cfg.total_node_count, 9)

    def test_env_overrides_files(self):
        cfg = config.load_config(cli(), env={"SHARDCOUNT": "5"})
        self.assertEqual(cfg.shard_count, 5)

    def test_cli_overrides_env(self):
        cfg = config.load_config(cli(shards="1"), env={"SHARDCOUNT": "5"})
        self.assertEqual(cfg.shard_count, 1)

    def test_num_validators_shorthand(self):
        cfg = config.load_config(cli(), env={"NUM_VALIDATORS": "4"})
        self.assertEqual(cfg.shard_validator_count, 4)
        self.assertEqual(cfg.meta_validator_count, 4)

    def test_explicit_count_beats_shorthand(self):
        cfg = config.load_config(
            cli(shard_validators="2"), env={"NUM_VALIDATORS": "4"})
        self.assertEqual(cfg.shard_validator_count, 2)
        self.assertEqual(cfg.meta_validator_count, 4)

    def test_consensus_clamped(self):
        cfg = config.load_config(cli(shard_validators="1", meta_validators="2"),
                                 env={})
        self.assertEqual(cfg.shard_consensus_size, 1)
        self.assertEqual(cfg.meta_consensus_size, 2)

    def test_loglevel_explicit(self):
        cfg = config.load_config(cli(), env={"LOGLEVEL": "*:INFO"})
        self.assertEqual(cfg.loglevel, "*:INFO")
        cfg = config.load_config(cli(log_level="*:TRACE"),
                                 env={"LOGLEVEL": "*:INFO"})
        self.assertEqual(cfg.loglevel, "*:TRACE")

    def test_testnet_dir(self):
        cfg = config.load_config(cli(), env={})
        self.assertTrue(cfg.testnet_dir.endswith(
            os.path.join("MultiversX", "testnet")))
        cfg = config.load_config(cli(), env={"TESTNETDIR": "/tmp/x"})
        self.assertEqual(cfg.testnet_dir, "/tmp/x")
        self.assertEqual(cfg.pid_dir, "/tmp/x/pids")
        self.assertEqual(cfg.log_dir, "/tmp/x/logs")

    def test_invalid_integer(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(), env={"SHARDCOUNT": "abc"})

    def test_genesis_delay_default_future(self):
        # Default comes from the shell files: +30 (genesis in the future).
        cfg = config.load_config(cli(), env={})
        self.assertEqual(cfg.genesis_delay, 30)

    def test_genesis_delay_explicit(self):
        cfg = config.load_config(cli(), env={"GENESIS_DELAY": "60"})
        self.assertEqual(cfg.genesis_delay, 60)
        cfg = config.load_config(cli(genesis_delay="-45"), env={})
        self.assertEqual(cfg.genesis_delay, -45)

    def test_genesis_delay_invalid(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(genesis_delay="soon"), env={})

    def test_node_delay_explicit(self):
        # variables.sh defaults NODE_DELAY to 60; env/CLI win over files.
        cfg = config.load_config(cli(), env={})
        self.assertEqual(cfg.node_delay, 60)
        cfg = config.load_config(cli(), env={"NODE_DELAY": "45"})
        self.assertEqual(cfg.node_delay, 45)
        cfg = config.load_config(
            cli(node_delay="10"), env={"NODE_DELAY": "45"})
        self.assertEqual(cfg.node_delay, 10)

    def test_supernova_round_explicit(self):
        cfg = config.load_config(cli(), env={"SUPERNOVA_ROUND": "50"})
        self.assertEqual(cfg.supernova_round, 50)
        cfg = config.load_config(
            cli(supernova_round="77"), env={"SUPERNOVA_ROUND": "50"})
        self.assertEqual(cfg.supernova_round, 77)

    def test_supernova_round_invalid(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(), env={"SUPERNOVA_ROUND": "soon"})
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(supernova_round="-1"), env={})

    def test_validation(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(shards="0"), env={})
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(proxy_port="99999"), env={})

    def test_print_config(self):
        cfg = config.load_config(
            cli(validators="1", shards="1"), env={"TESTNETDIR": "/tmp/t"})
        out = config.format_print_config(cfg)
        self.assertIn("SHARDCOUNT=1 SHARD_VALIDATORCOUNT=1", out)
        self.assertIn("SHARD_CONSENSUS_SIZE=1 META_CONSENSUS_SIZE=1", out)
        self.assertIn("LOGLEVEL=*:DEBUG", out)
        self.assertIn("logs=/tmp/t/logs", out)

    def test_validator_slots_meta_first(self):
        # nodesSetup.json is positional with metachain entries first
        # (processMetaChainAssigment), so metachain takes indices 0..M-1.
        cfg = config.load_config(
            cli(shards="2", shard_validators="2", meta_validators="1"),
            env={},
        )
        self.assertEqual(
            cfg.validator_slots(),
            [
                (0, "meta", 4294967295),
                (1, "shard", 0),
                (2, "shard", 0),
                (3, "shard", 1),
                (4, "shard", 1),
            ],
        )


class TxgenConfigTest(unittest.TestCase):
    def test_txgen_defaults(self):
        cfg = config.load_config(cli(), env={})
        self.assertEqual(cfg.txgen_port, 7951)
        self.assertEqual(cfg.txgen_num_accounts, 250)
        self.assertEqual(cfg.txgen_scenarios, ["basic", "erc20", "esdt"])
        self.assertTrue(cfg.txgen_bulk_enabled)
        self.assertEqual(cfg.txgen_bulk_size, 500)
        self.assertEqual(cfg.txgen_bulk_interval_ms, 10000)
        self.assertTrue(
            cfg.txgen_dir.endswith(
                os.path.join("mx-chain-txgen-go", "cmd", "txgen")))
        self.assertEqual(
            cfg.txgen_bin,
            os.path.join(cfg.testnet_dir, "txgen", "txgen"))

    def test_txgen_cli_overrides(self):
        cfg = config.load_config(
            cli(txgen_port="7959", txgen_accounts="10",
                txgen_scenarios="basic,esdt",
                txgen_bulk_enabled="0",
                txgen_bulk_size="100",
                txgen_bulk_interval_ms="2000"),
            env={},
        )
        self.assertEqual(cfg.txgen_port, 7959)
        self.assertEqual(cfg.txgen_num_accounts, 10)
        self.assertEqual(cfg.txgen_scenarios, ["basic", "esdt"])
        self.assertFalse(cfg.txgen_bulk_enabled)
        self.assertEqual(cfg.txgen_bulk_size, 100)
        self.assertEqual(cfg.txgen_bulk_interval_ms, 2000)

    def test_txgen_scenarios_toml_line(self):
        cfg = config.load_config(
            cli(txgen_scenarios='Scenarios = ["basic", "esdt"]'), env={})
        self.assertEqual(cfg.txgen_scenarios, ["basic", "esdt"])

    def test_txgen_env_overrides_files(self):
        cfg = config.load_config(cli(), env={"PORT_TXGEN": "7961"})
        self.assertEqual(cfg.txgen_port, 7961)
        cfg = config.load_config(cli(), env={"NUM_ACCOUNTS": "11"})
        self.assertEqual(cfg.txgen_num_accounts, 11)
        cfg = config.load_config(
            cli(), env={"NUMACCOUNTS": "12", "NUM_ACCOUNTS": "11"})
        self.assertEqual(cfg.txgen_num_accounts, 12)

    def test_txgen_validation(self):
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(txgen_port="99999"), env={})
        with self.assertRaises(config.ConfigError):
            config.load_config(cli(txgen_accounts="0"), env={})
        with self.assertRaises(config.ConfigError):
            config.load_config(
                cli(txgen_bulk_size="0"), env={})

    def test_print_config_includes_txgen(self):
        cfg = config.load_config(cli(), env={"TESTNETDIR": "/tmp/t"})
        out = config.format_print_config(cfg)
        self.assertIn("TXGEN port=7951", out)
        self.assertIn("scenarios=[basic,erc20,esdt]", out)


if __name__ == "__main__":
    unittest.main()
