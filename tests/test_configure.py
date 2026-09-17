#!/usr/bin/env python3
"""Tests for configure.py: TOML/JSON edits and proxy rendering."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import configure
import config as config_mod


def chain_node_config_dir():
    """cmd/node/config dir of the auto-detected mx-chain-go checkout."""
    chain = config_mod.resolve_chain_root({}, {})
    return os.path.join(chain, "cmd", "node", "config")

SAMPLE_EPOCHS = """\
    StakingV4Step3EnableEpoch = 3
    MaxNodesChangeEnableEpoch = [
        { EpochEnable = 0, MaxNumNodes = 48, NodesToShufflePerShard = 4 },
        { EpochEnable = 1, MaxNumNodes = 64, NodesToShufflePerShard = 2 },
        # Staking v4 configuration
        { EpochEnable = 3, MaxNumNodes = 56, NodesToShufflePerShard = 2 },
    ]
"""


class TomlTest(unittest.TestCase):
    def test_set_value(self):
        text = '    ChainID = "undefined"\n    Other = 1\n'
        self.assertEqual(
            configure.set_toml_value(text, "ChainID", '"local-testnet"'),
            '    ChainID = "local-testnet"\n    Other = 1\n',
        )

    def test_skips_comments(self):
        text = '    # ChainID = "old"\n    ChainID = "undefined"\n'
        out = configure.set_toml_value(text, "ChainID", '"x"')
        self.assertIn('# ChainID = "old"', out)
        self.assertIn('ChainID = "x"', out)

    def test_missing_key_unchanged(self):
        self.assertEqual(configure.set_toml_value("a = 1\n", "Nope", "2"), "a = 1\n")

    def test_empty_cpu_flags(self):
        text = "[HardwareRequirements]\n    CPUFlags = [\"SSE4\", \"SSE42\"]\n"
        self.assertEqual(
            configure.empty_cpu_flags(text),
            "[HardwareRequirements]\n    CPUFlags = []\n",
        )


class JsonTest(unittest.TestCase):
    def test_set_values(self):
        text = '  "startTime": 111,\n  "minTransactionVersion": "1",\n'
        out = configure.set_json_value(text, "startTime", "222")
        self.assertIn('"startTime": 222,', out)


class ChainParamsTest(unittest.TestCase):
    BASE = (
        "    ShardConsensusGroupSize = 3, ShardMinNumNodes = 3,\n"
        "    MetachainConsensusGroupSize = 3, MetachainMinNumNodes = 3,\n"
        "    RoundDuration = 6000,\n"
        "    Hysteresis = 0.0,\n"
        "    RoundsPerEpoch = 2000,\n"
        "    RoundsPerEpoch = 200,\n"
        "    MinRoundsBetweenEpochs = 20,\n"
    )

    def test_without_rounds_override(self):
        out, warnings = configure.apply_chain_params(self.BASE, 1, 1, 6000, 0.0, 0)
        self.assertIn("ShardConsensusGroupSize = 1", out)
        self.assertIn("MetachainMinNumNodes = 1", out)
        self.assertIn("RoundsPerEpoch = 2000", out)  # untouched
        self.assertEqual(warnings, [])

    def test_with_rounds_override(self):
        out, warnings = configure.apply_chain_params(self.BASE, 1, 1, 6000, 0.0, 50)
        self.assertIn("RoundsPerEpoch = 500", out)  # 50 * 6000 // 600
        self.assertIn("RoundsPerEpoch = 50", out)
        self.assertIn("MinRoundsBetweenEpochs = 50", out)
        self.assertEqual(warnings, [])

    def test_multi_line_config(self):
        # Mirrors the real config.toml: one parameter set per EnableEpoch
        # line; sed (no "g" flag) rewrites the first match of EVERY line.
        text = (
            "        { EnableEpoch = 0, ShardConsensusGroupSize = 10,"
            " Hysteresis = 0.2 },\n"
            "        { EnableEpoch = 1, ShardConsensusGroupSize = 10,"
            " Hysteresis = 0.2 },\n"
            "        { EnableEpoch = 2, ShardConsensusGroupSize = 10,"
            " Hysteresis = 0.2 },\n"
        )
        out, warnings = configure.apply_chain_params(text, 1, 1, 6000, 0.0, 0)
        self.assertEqual(out.count("ShardConsensusGroupSize = 1"), 3)
        self.assertEqual(out.count("Hysteresis = 0.0"), 3)
        self.assertNotIn("10", out)
        self.assertNotIn("0.2", out)
        # RoundDuration = 6000 absent from this stub → warning is expected.
        self.assertTrue(any("RoundDuration" in w for w in warnings))


class StakingV4Test(unittest.TestCase):
    def test_one_shard(self):
        updated, message = configure.update_staking_v4_max_nodes(SAMPLE_EPOCHS, 1)
        self.assertIn("{ EpochEnable = 3, MaxNumNodes = 60", updated)
        self.assertIn("60", message)

    def test_three_shards_unchanged_value(self):
        updated, _message = configure.update_staking_v4_max_nodes(SAMPLE_EPOCHS, 3)
        self.assertIn("{ EpochEnable = 3, MaxNumNodes = 56", updated)

    def test_too_few_entries(self):
        text = "MaxNodesChangeEnableEpoch = [\n]\n"
        updated, message = configure.update_staking_v4_max_nodes(text, 1)
        self.assertEqual(updated, text)
        self.assertIn("Not enough entries", message)

    def test_missing_step3_entry_warns(self):
        text = SAMPLE_EPOCHS.replace("StakingV4Step3EnableEpoch = 3",
                                     "StakingV4Step3EnableEpoch = 9")
        updated, message = configure.update_staking_v4_max_nodes(text, 1)
        self.assertEqual(updated, text)
        self.assertIn("Warning", message)

    def test_real_repo_file(self):
        repo_file = os.path.join(
            chain_node_config_dir(), "enableEpochs.toml"
        )
        if not os.path.isfile(repo_file):
            self.skipTest("repo enableEpochs.toml not found")
        with open(repo_file, encoding="utf-8") as handle:
            text = handle.read()
        updated, _message = configure.update_staking_v4_max_nodes(text, 1)
        self.assertIn("{ EpochEnable = 3, MaxNumNodes = 60", updated)


class ProxyTest(unittest.TestCase):
    def test_truncate(self):
        text = "[GeneralSettings]\n   ServerPort = 8080\n[[Observers]]\n   ShardId = 0\n"
        out = configure.truncate_proxy_observers(text)
        self.assertNotIn("[[Observers]]", out)
        self.assertIn("ServerPort = 8080", out)

    def test_truncate_without_marker(self):
        text = "[GeneralSettings]\n   ServerPort = 8080\n"
        self.assertEqual(configure.truncate_proxy_observers(text), text)

    def test_render(self):
        # Shard entries come first (the proxy asks observers in file order
        # for the shard count); ports follow the meta-first slot mapping.
        slots = [(0, "meta", 4294967295), (1, "shard", 0)]
        self.assertEqual(
            configure.render_proxy_observers(slots, 9500),
            '[[Observers]]\n'
            '   ShardId = 0\n'
            '   Address = "http://127.0.0.1:9501"\n'
            "\n"
            '[[Observers]]\n'
            "   ShardId = 4294967295\n"
            '   Address = "http://127.0.0.1:9500"\n'
            "\n",
        )

    def test_render_two_shards(self):
        slots = [
            (0, "meta", 4294967295),
            (1, "shard", 0),
            (2, "shard", 0),
            (3, "shard", 1),
            (4, "shard", 1),
        ]
        out = configure.render_proxy_observers(slots, 9500)
        shards = []
        for line in out.splitlines():
            if "ShardId" in line:
                shards.append(int(line.split("=")[1]))
        self.assertEqual(shards, [0, 0, 1, 1, 4294967295])
        for port in range(9500, 9505):
            self.assertIn("http://127.0.0.1:%d" % port, out)


class SupernovaTest(unittest.TestCase):
    BASE = (
        "        { EnableEpoch = 0, RoundDuration = 6000, RoundsPerEpoch = 200, MinRoundsBetweenEpochs = 20 },\n"
        "        { EnableRound = 0, MaxRoundsWithoutNewBlockReceived = 10 },\n"
        "        { EnableRound = 440, MaxRoundsWithoutNewBlockReceived = 100 },\n"
        "        { EnableRound = 0, MaxRoundsWithoutCommittedStartInEpochBlock = 50 },\n"
        "        { EnableRound = 440, MaxRoundsWithoutCommittedStartInEpochBlock = 500 },\n"
        "        { EnableRound = 0, SubroundsTiming = [] },\n"
        "        { EnableRound = 440, SubroundsTiming = [] },\n"
        "        { StartEpoch = 2, StartRound = 440, Version = \"3\" },\n"
        "        Round = 0\n"
        "        Round = 440\n"
        "    SizeInBytes = 26214400  # 25MB per each pair (metachain, destinationShard)\n"
    )

    def test_rewrites_all_round_entries(self):
        out, warnings = configure.apply_supernova_round(self.BASE, 50)
        self.assertEqual(out.count("EnableRound = 50"), 3)
        self.assertIn('StartRound = 50, Version = "3"', out)
        self.assertIn("\n        Round = 50\n", out)
        self.assertNotIn("EnableRound = 440", out)
        self.assertNotIn("StartRound = 440", out)
        self.assertNotIn("Round = 440\n", out)
        # Unrelated numbers containing 440 must stay untouched.
        self.assertIn("SizeInBytes = 26214400", out)
        self.assertEqual(warnings, [])

    def test_default_round_is_noop(self):
        out, warnings = configure.apply_supernova_round(self.BASE, 440)
        self.assertEqual(out, self.BASE)
        self.assertEqual(warnings, [])

    def test_enable_rounds(self):
        text = (
            '    [RoundActivations.SupernovaEnableRound]\n'
            '        Options = []\n'
            '        Round = "440"\n'
        )
        out = configure.apply_supernova_enable_round(text, 50)
        self.assertIn('Round = "50"', out)
        self.assertNotIn("440", out)

    def test_enable_rounds_leaves_other_rounds(self):
        text = '        Round = "0"\n        Round = "440"\n'
        out = configure.apply_supernova_enable_round(text, 50)
        self.assertIn('Round = "0"', out)
        self.assertIn('Round = "50"', out)

    def test_real_repo_files(self):
        # Version-tolerant: old configs carry 440-markers that must all move
        # together; newer configs without markers must pass through untouched.
        node_config = chain_node_config_dir()
        config_toml = os.path.join(node_config, "config.toml")
        rounds_toml = os.path.join(node_config, "enableRounds.toml")
        if not os.path.isfile(config_toml) or not os.path.isfile(rounds_toml):
            self.skipTest("repo config files not found")
        with open(config_toml, encoding="utf-8") as handle:
            text = handle.read()
        out, warnings = configure.apply_supernova_round(text, 50)
        self.assertNotIn("EnableRound = 440", out)
        self.assertNotIn("StartRound = 440", out)
        self.assertNotIn("\n        Round = 440", out)
        self.assertEqual(out.count("EnableRound = 50"), text.count("EnableRound = 440"))
        self.assertEqual(out.count("StartRound = 50"), text.count("StartRound = 440"))
        if "26214400" in text:
            self.assertIn("26214400", out)  # unrelated numbers never touched
        with open(rounds_toml, encoding="utf-8") as handle:
            text = handle.read()
        out = configure.apply_supernova_enable_round(text, 50)
        self.assertNotIn('Round = "440"', out)
        self.assertEqual(
            out.count('Round = "50"'),
            text.count('Round = "440"') + text.count('Round = "50"'),
        )


class P2pTest(unittest.TestCase):
    def test_parse(self):
        line = "-----BEGIN PRIVATE KEY for 16Uiu2HAkwABC-----"
        self.assertEqual(configure.p2p_pubkey_from_first_line(line),
                         "16Uiu2HAkwABC")

    def test_invalid(self):
        with self.assertRaises(ValueError):
            configure.p2p_pubkey_from_first_line("garbage line")


class TxgenTest(unittest.TestCase):
    def test_parse_full_line(self):
        self.assertEqual(
            configure.parse_txgen_scenarios('Scenarios = ["basic", "erc20", "esdt"]'),
            ["basic", "erc20", "esdt"],
        )

    def test_parse_bare_list(self):
        self.assertEqual(
            configure.parse_txgen_scenarios('["basic", "esdt"]'),
            ["basic", "esdt"],
        )

    def test_parse_comma_separated(self):
        self.assertEqual(
            configure.parse_txgen_scenarios("basic,erc20,esdt"),
            ["basic", "erc20", "esdt"],
        )

    def test_render(self):
        self.assertEqual(
            configure.render_txgen_scenarios(["basic", "esdt"]),
            'Scenarios = ["basic", "esdt"]',
        )

    def test_apply(self):
        text = (
            "[GeneralSettings]\n"
            "    ServerPort = 7998\n"
            'Scenarios = ["basic"]\n'
            "[Proxy]\n"
            '    ProxyServerURL = "http://127.0.0.1:8080"\n'
        )
        out = configure.apply_txgen_config(text, 7951, 7950, ["basic", "esdt"])
        self.assertIn("ServerPort = 7951", out)
        self.assertIn('ProxyServerURL = "http://127.0.0.1:7950"', out)
        self.assertIn('Scenarios = ["basic", "esdt"]', out)
        self.assertNotIn("7998", out)
        self.assertNotIn("8080", out)

    def test_apply_skips_commented_scenarios(self):
        text = '    # Scenarios = ["basic"]\nScenarios = ["basic"]\n'
        out = configure.apply_txgen_config(text, 7951, 7950, ["esdt"])
        self.assertIn('# Scenarios = ["basic"]', out)
        self.assertIn('Scenarios = ["esdt"]', out)


class DbLookupExtensionTest(unittest.TestCase):
    BASE = (
        "[StoragePruning]\n"
        "    Enabled = true\n"
        "\n"
        "[DbLookupExtensions]\n"
        "    Enabled = false\n"
        "    DbLookupMaxActivePersisters = 10\n"
        "\n"
        "[Other]\n"
        "    Enabled = false\n"
    )

    def test_enables_only_its_section(self):
        out = configure.enable_db_lookup_extension(self.BASE)
        self.assertIn("[DbLookupExtensions]\n    Enabled = true\n", out)
        self.assertIn("[StoragePruning]\n    Enabled = true\n", out)
        self.assertIn("[Other]\n    Enabled = false\n", out)

    def test_idempotent(self):
        once = configure.enable_db_lookup_extension(self.BASE)
        self.assertEqual(configure.enable_db_lookup_extension(once), once)

    def test_missing_section_unchanged(self):
        text = "[GeneralSettings]\n    Enabled = false\n"
        self.assertEqual(configure.enable_db_lookup_extension(text), text)

    def test_real_repo_file(self):
        config_toml = os.path.join(chain_node_config_dir(), "config.toml")
        if not os.path.isfile(config_toml):
            self.skipTest("repo config file not found")
        with open(config_toml, encoding="utf-8") as handle:
            out = configure.enable_db_lookup_extension(handle.read())
        section = out.split("[DbLookupExtensions]")[1].split("[", 1)[0]
        self.assertIn("Enabled = true", section)


if __name__ == "__main__":
    unittest.main()
