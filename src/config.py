#!/usr/bin/env python3
"""Configuration loading for the Python local-testnet launcher.

This is a standalone repo (no longer nested under mx-chain-go). It drives
an mx-chain-go checkout whose location resolves as:
CLI ``--mx-chain-go-dir`` > env ``MX_CHAIN_GO_DIR`` > sibling checkouts
(``multiversx/mx-chain-go`` next to the Go workspace root).

From that checkout it reads:

1. static ``export NAME=value`` assignments from
   ``scripts/testnet/variables.sh`` (the shared Linux defaults),
2. ``scripts/testnet/local.sh`` applied on top when present
   (personal, gitignored overrides),
3. environment variables win over files,
4. CLI arguments win over everything,
5. local-testnet rules are applied (proxy forced on and wired to
   validators, ``*:DEBUG`` log level, consensus clamping, ...).

Only static assignments are parsed from the shell files; computed values
(``$(...)``, ``$VAR`` references, ``let`` arithmetic, ``if`` blocks) are
recomputed here in Python.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

import configure

# This file lives in <scripts-repo>/src/.
SCRIPTS_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Name of the env var / CLI flag selecting the mx-chain-go checkout.
CHAIN_GO_DIR_ENV = "MX_CHAIN_GO_DIR"


def _workspace_github_dir() -> str:
    # <workspace>/github.com/<org>/<repo> -> <workspace>/github.com
    return os.path.dirname(os.path.dirname(SCRIPTS_ROOT))


def default_chain_roots() -> List[str]:
    """Sibling mx-chain-go checkouts, most-preferred first."""
    github = _workspace_github_dir()
    return [
        os.path.join(github, "multiversx", "mx-chain-go"),
    ]


def resolve_chain_root(
    cli: Mapping[str, Optional[str]],
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve the mx-chain-go checkout this tool drives.

    Precedence: CLI ``--mx-chain-go-dir`` > env ``MX_CHAIN_GO_DIR`` >
    sibling checkouts (``multiversx/mx-chain-go``).
    """
    if env is None:
        env = os.environ
    explicit = _non_empty(cli, "mx_chain_go_dir")
    if explicit is None:
        explicit = _non_empty(env, CHAIN_GO_DIR_ENV)
    if explicit is not None:
        root = os.path.expanduser(explicit)
        if not os.path.isdir(os.path.join(root, "cmd", "node")):
            raise ConfigError(
                "not an mx-chain-go checkout (no cmd/node): %s" % root
            )
        return root
    for candidate in default_chain_roots():
        if os.path.isdir(os.path.join(candidate, "cmd", "node")):
            return candidate
    raise ConfigError(
        "no mx-chain-go checkout found (tried %s). Set --mx-chain-go-dir "
        "or %s." % (", ".join(default_chain_roots()), CHAIN_GO_DIR_ENV)
    )


def chain_scripts_dir(chain_root: str) -> str:
    return os.path.join(chain_root, "scripts", "testnet")

DEFAULT_LOGLEVEL = "*:DEBUG"
APP_VERSION = "v1.0.0-mac"

_EXPORT_RE = re.compile(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")




class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


def _strip_value(raw: str) -> str:
    raw = raw.strip()
    if raw[:1] in ("'", '"') and len(raw) >= 2:
        quote = raw[0]
        end = raw.find(quote, 1)
        if end != -1:
            return raw[1:end]
    return raw.split("#", 1)[0].strip()


def parse_shell_exports(path: str) -> Dict[str, str]:
    """Parse static ``export NAME=value`` lines from a shell file.

    Lines whose value is computed dynamically (``$VAR``, ``$(...)``,
    backquotes) are skipped; comments and blank lines are ignored.
    """
    values: Dict[str, str] = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            match = _EXPORT_RE.match(line)
            if not match:
                continue
            name, raw = match.group(1), match.group(2).strip()
            if "$" in raw or "`" in raw:
                continue
            values[name] = _strip_value(raw)
    return values


def _as_int(values: Mapping[str, str], key: str, default: int) -> int:
    raw = values.get(key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError("invalid integer for %s: %r" % (key, raw))


def _as_float(values: Mapping[str, str], key: str, default: float) -> float:
    raw = values.get(key, "")
    if raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError("invalid float for %s: %r" % (key, raw))


def _as_bool(values: Mapping[str, str], key: str, default: bool) -> bool:
    raw = values.get(key, "")
    if str(raw).strip() == "":
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _sibling_checkout(chain_root: str, repo: str) -> str:
    """Locate a sibling tooling checkout for the chain root.

    Prefers a checkout next to the chain itself (same org), falling back
    to ``multiversx/<repo>`` under the Go workspace root, where shared
    tooling checkouts usually live.
    """
    near = os.path.join(os.path.dirname(chain_root), repo)
    if os.path.isdir(near):
        return near
    alt = os.path.join(_workspace_github_dir(), "multiversx", repo)
    if os.path.isdir(alt):
        return alt
    return near


def _default_txgen_dir(repo_root: str) -> str:
    """Locate ``mx-chain-txgen-go/cmd/txgen`` next to the chain repo."""
    return os.path.join(_sibling_checkout(repo_root, "mx-chain-txgen-go"), "cmd", "txgen")


def _non_empty(mapping: Mapping[str, Optional[str]], key: str) -> Optional[str]:
    value = mapping.get(key)
    if value is None or str(value) == "":
        return None
    return str(value)


@dataclass
class TestnetConfig:
    """Fully resolved testnet configuration."""

    testnet_dir: str
    repo_root: str
    filegen_dir: str
    filegen_output_dir: str
    filegen_bin: str
    seednode_dir: str
    seednode_bin: str
    node_dir: str
    node_bin: str
    proxy_dir: str
    proxy_bin: str
    keygen_dir: str

    shard_count: int
    shard_validator_count: int
    meta_validator_count: int
    shard_consensus_size: int
    meta_consensus_size: int
    total_node_count: int

    metashard_id: int
    seednode_ip: str
    seednode_port: int
    validator_port_origin: int
    validator_rest_origin: int
    proxy_port: int

    seednode_delay: int
    genesis_delay: int
    node_delay: int
    proxy_delay: int

    genesis_stake_type: str
    round_duration_ms: int
    hysteresis: float
    rounds_per_epoch: int
    supernova_round: int
    always_new_chainid: int
    loglevel: str
    node_niceness: Optional[int]

    txgen_dir: str = ""
    txgen_bin: str = ""
    txgen_port: int = 7951
    txgen_scenarios: List[str] = field(default_factory=list)
    txgen_num_accounts: int = 250
    txgen_bulk_enabled: bool = True
    txgen_bulk_size: int = 500
    txgen_bulk_interval_ms: int = 10000

    use_proxy: bool = True
    proxy_use_validators: bool = True

    pid_dir: str = ""
    log_dir: str = ""

    def validator_p2p_port(self, index: int) -> int:
        return self.validator_port_origin + index

    def validator_rest_port(self, index: int) -> int:
        return self.validator_rest_origin + index

    def validator_count(self) -> int:
        return (
            self.shard_count * self.shard_validator_count
            + self.meta_validator_count
        )

    def validator_slots(self) -> List[Tuple[int, str, int]]:
        """Return ``(global_index, kind, shard_id)`` for every validator.

        The node assigns ``nodesSetup.json`` entries positionally with the
        metachain entries FIRST (see ``processMetaChainAssigment`` in
        ``sharding/nodesSetup.go``), so metachain validators take the first
        indices however many shards exist. ``kind`` is ``"meta"`` or
        ``"shard"``; ``shard_id`` is the metashard id for meta validators.
        """
        slots: List[Tuple[int, str, int]] = []
        for _ in range(self.meta_validator_count):
            slots.append((len(slots), "meta", self.metashard_id))
        for shard in range(self.shard_count):
            for _ in range(self.shard_validator_count):
                slots.append((len(slots), "shard", shard))
        return slots


def load_config(
    cli: Mapping[str, Optional[str]],
    env: Optional[Mapping[str, str]] = None,
) -> TestnetConfig:
    """Resolve configuration with CLI > env > local.sh > variables.sh."""
    if env is None:
        env = os.environ
    chain_root = resolve_chain_root(cli, env)
    scripts_dir = chain_scripts_dir(chain_root)
    file_values: Dict[str, str] = {}
    base_variables = os.path.join(scripts_dir, "variables.sh")
    local_overrides = os.path.join(scripts_dir, "local.sh")
    if os.path.isfile(base_variables):
        file_values.update(parse_shell_exports(base_variables))
    if os.path.isfile(local_overrides):
        file_values.update(parse_shell_exports(local_overrides))

    merged: Dict[str, str] = dict(file_values)
    for key in (
        "NUM_VALIDATORS",
        "TESTNETDIR",
        "SHARDCOUNT",
        "SHARD_VALIDATORCOUNT",
        "META_VALIDATORCOUNT",
        "PORT_PROXY",
        "LOGLEVEL",
        "GENESIS_DELAY",
        "SUPERNOVA_ROUND",
        "ROUNDS_PER_EPOCH",
        "NODE_DELAY",
        "PORT_TXGEN",
        "NUMACCOUNTS",
        "TXGEN_SCENARIOS",
        "TXGEN_BULK_ENABLED",
        "TXGEN_BULK_SIZE",
        "TXGEN_BULK_INTERVAL_MS",
        "TXGENDIR",
    ):
        value = _non_empty(env, key)
        if value is not None:
            merged[key] = value
    for key, flag in (
        ("NUM_VALIDATORS", "validators"),
        ("SHARDCOUNT", "shards"),
        ("SHARD_VALIDATORCOUNT", "shard_validators"),
        ("META_VALIDATORCOUNT", "meta_validators"),
        ("PORT_PROXY", "proxy_port"),
        ("LOGLEVEL", "log_level"),
        ("GENESIS_DELAY", "genesis_delay"),
        ("SUPERNOVA_ROUND", "supernova_round"),
        ("ROUNDS_PER_EPOCH", "rounds_per_epoch"),
        ("NODE_DELAY", "node_delay"),
        ("TESTNETDIR", "testnet_dir"),
        ("PORT_TXGEN", "txgen_port"),
        ("NUMACCOUNTS", "txgen_accounts"),
        ("TXGEN_SCENARIOS", "txgen_scenarios"),
        ("TXGEN_BULK_ENABLED", "txgen_bulk_enabled"),
        ("TXGEN_BULK_SIZE", "txgen_bulk_size"),
        ("TXGEN_BULK_INTERVAL_MS", "txgen_bulk_interval_ms"),
        ("TXGENDIR", "txgen_dir"),
    ):
        value = _non_empty(cli, flag)
        if value is not None:
            merged[key] = value
    # Legacy underscore alias used by txgen's shell helper (NUM_ACCOUNTS).
    # Precedence: CLI --txgen-accounts > env NUMACCOUNTS > env NUM_ACCOUNTS
    # > shell files > default. The generic loops above already applied the
    # first two, so only fill from the alias when neither was given.
    if (
        _non_empty(cli, "txgen_accounts") is None
        and _non_empty(env, "NUMACCOUNTS") is None
        and _non_empty(env, "NUM_ACCOUNTS") is not None
    ):
        merged["NUMACCOUNTS"] = str(env["NUM_ACCOUNTS"])

    def cli_or_env_set(flag: str, env_key: str) -> bool:
        return _non_empty(cli, flag) is not None or _non_empty(env, env_key) is not None

    # NUM_VALIDATORS shorthand fills the counts the user did not set directly.
    num_validators = _non_empty(merged, "NUM_VALIDATORS")
    if num_validators is not None:
        if not cli_or_env_set("shard_validators", "SHARD_VALIDATORCOUNT"):
            merged["SHARD_VALIDATORCOUNT"] = num_validators
        if not cli_or_env_set("meta_validators", "META_VALIDATORCOUNT"):
            merged["META_VALIDATORCOUNT"] = num_validators

    shard_count = _as_int(merged, "SHARDCOUNT", 3)
    shard_validators = _as_int(merged, "SHARD_VALIDATORCOUNT", 3)
    meta_validators = _as_int(merged, "META_VALIDATORCOUNT", 3)
    shard_consensus = _as_int(merged, "SHARD_CONSENSUS_SIZE", 3)
    meta_consensus_raw = merged.get("META_CONSENSUS_SIZE", "")
    meta_consensus = (
        int(meta_consensus_raw)
        if meta_consensus_raw not in ("", None)
        and str(meta_consensus_raw).lstrip("-").isdigit()
        else meta_validators
    )
    # Consensus can never exceed the number of validators.
    shard_consensus = min(shard_consensus, shard_validators)
    meta_consensus = min(meta_consensus, meta_validators)

    # No observers: the proxy talks directly to the validators.
    total_nodes = shard_count * shard_validators + meta_validators

    repo_root = chain_root
    filegen_dir = merged.get(
        "CONFIGGENERATORDIR",
        os.path.join(
            _sibling_checkout(repo_root, "mx-chain-deploy-go"), "cmd", "filegen"
        ),
    )
    proxy_dir = merged.get(
        "PROXYDIR",
        os.path.join(
            _sibling_checkout(repo_root, "mx-chain-proxy-go"), "cmd", "proxy"
        ),
    )
    testnet_dir = merged.get(
        "TESTNETDIR", os.path.join(os.path.expanduser("~"), "MultiversX", "testnet")
    )

    niceness_raw = merged.get("NODE_NICENESS", "")
    node_niceness: Optional[int] = None
    if str(niceness_raw).strip() != "":
        try:
            node_niceness = int(str(niceness_raw).strip())
        except ValueError:
            raise ConfigError("invalid integer for NODE_NICENESS: %r" % niceness_raw)

    txgen_dir = merged.get("TXGENDIR", "") or _default_txgen_dir(repo_root)
    scenarios_raw = (
        merged.get("TXGEN_SCENARIOS", "")
        or merged.get("TXGEN_SCENARIOS_LINE", "")
        or 'Scenarios = ["basic", "erc20", "esdt"]'
    )
    txgen_scenarios = configure.parse_txgen_scenarios(scenarios_raw)
    if not txgen_scenarios:
        raise ConfigError("no txgen scenarios resolved from %r" % scenarios_raw)

    cfg = TestnetConfig(
        testnet_dir=testnet_dir,
        repo_root=repo_root,
        filegen_dir=filegen_dir,
        filegen_output_dir=merged.get("CONFIGGENERATOROUTPUTDIR", "output"),
        filegen_bin=os.path.join(testnet_dir, "filegen", "filegen"),
        seednode_dir=os.path.join(repo_root, "cmd", "seednode"),
        seednode_bin=os.path.join(testnet_dir, "seednode", "seednode"),
        node_dir=os.path.join(repo_root, "cmd", "node"),
        node_bin=os.path.join(testnet_dir, "node", "node"),
        proxy_dir=proxy_dir,
        proxy_bin=os.path.join(testnet_dir, "proxy", "proxy"),
        keygen_dir=os.path.join(repo_root, "cmd", "keygenerator"),
        shard_count=shard_count,
        shard_validator_count=shard_validators,
        meta_validator_count=meta_validators,
        shard_consensus_size=shard_consensus,
        meta_consensus_size=meta_consensus,
        total_node_count=total_nodes,
        metashard_id=_as_int(merged, "METASHARD_ID", 4294967295),
        seednode_ip=merged.get("SEEDNODE_IP", "127.0.0.1"),
        seednode_port=_as_int(merged, "PORT_SEEDNODE", 9999),
        validator_port_origin=_as_int(merged, "PORT_ORIGIN_VALIDATOR", 21500),
        validator_rest_origin=_as_int(merged, "PORT_ORIGIN_VALIDATOR_REST", 9500),
        proxy_port=_as_int(merged, "PORT_PROXY", 7950),
        seednode_delay=_as_int(merged, "SEEDNODE_DELAY", 5),
        genesis_delay=_as_int(merged, "GENESIS_DELAY", 30),
        node_delay=_as_int(merged, "NODE_DELAY", 10),
        proxy_delay=_as_int(merged, "PROXY_DELAY", 10),
        genesis_stake_type=merged.get("GENESIS_STAKE_TYPE", "direct"),
        round_duration_ms=_as_int(merged, "ROUND_DURATION_IN_MS", 6000),
        hysteresis=_as_float(merged, "HYSTERESIS", 0.0),
        rounds_per_epoch=_as_int(merged, "ROUNDS_PER_EPOCH", 0),
        supernova_round=_as_int(merged, "SUPERNOVA_ROUND", 440),
        always_new_chainid=_as_int(merged, "ALWAYS_NEW_CHAINID", 1),
        loglevel=merged.get("LOGLEVEL", DEFAULT_LOGLEVEL),
        node_niceness=node_niceness,
        txgen_dir=txgen_dir,
        txgen_bin=os.path.join(testnet_dir, "txgen", "txgen"),
        txgen_port=_as_int(merged, "PORT_TXGEN", 7951),
        txgen_scenarios=txgen_scenarios,
        txgen_num_accounts=_as_int(merged, "NUMACCOUNTS", 250),
        txgen_bulk_enabled=_as_bool(merged, "TXGEN_BULK_ENABLED", True),
        txgen_bulk_size=_as_int(merged, "TXGEN_BULK_SIZE", 500),
        txgen_bulk_interval_ms=_as_int(merged, "TXGEN_BULK_INTERVAL_MS", 10000),
        pid_dir=os.path.join(testnet_dir, "pids"),
        log_dir=os.path.join(testnet_dir, "logs"),
    )
    # variables.sh unconditionally defaults LOGLEVEL to "*:INFO"; only honor
    # it when the user set it explicitly via CLI or environment.
    if _non_empty(cli, "log_level") is not None:
        cfg.loglevel = str(cli["log_level"])
    elif _non_empty(env, "LOGLEVEL") is not None:
        cfg.loglevel = str(env["LOGLEVEL"])
    elif cfg.loglevel == "*:INFO":
        cfg.loglevel = DEFAULT_LOGLEVEL

    _validate(cfg)
    return cfg


def _validate(cfg: TestnetConfig) -> None:
    errors = []
    if cfg.shard_count < 1:
        errors.append("SHARDCOUNT must be >= 1")
    if cfg.shard_validator_count < 1:
        errors.append("SHARD_VALIDATORCOUNT must be >= 1")
    if cfg.meta_validator_count < 1:
        errors.append("META_VALIDATORCOUNT must be >= 1")
    for name in ("seednode_port", "validator_port_origin",
                 "validator_rest_origin", "proxy_port", "txgen_port"):
        port = getattr(cfg, name)
        if not 1 <= port <= 65535:
            errors.append("%s must be a valid port, got %d" % (name, port))
    for name in ("seednode_delay", "node_delay", "proxy_delay"):
        if getattr(cfg, name) < 0:
            errors.append("%s must be >= 0" % name)
    if cfg.supernova_round < 0:
        errors.append("supernova_round must be >= 0")
    if cfg.rounds_per_epoch < 0:
        errors.append("rounds_per_epoch must be >= 0")
    if cfg.txgen_num_accounts < 1:
        errors.append("NUMACCOUNTS must be >= 1")
    if cfg.txgen_bulk_enabled:
        if cfg.txgen_bulk_size < 1:
            errors.append("TXGEN_BULK_SIZE must be >= 1")
        if cfg.txgen_bulk_interval_ms < 1:
            errors.append("TXGEN_BULK_INTERVAL_MS must be >= 1")
    if errors:
        raise ConfigError("; ".join(errors))


def format_print_config(cfg: TestnetConfig) -> str:
    """Render the ``--print-config`` output (same lines as mac/start.sh)."""
    last = cfg.validator_count() - 1
    lines = [
        "resolved: SHARDCOUNT=%d SHARD_VALIDATORCOUNT=%d META_VALIDATORCOUNT=%d"
        % (cfg.shard_count, cfg.shard_validator_count, cfg.meta_validator_count),
        "resolved: SHARD_CONSENSUS_SIZE=%d META_CONSENSUS_SIZE=%d TOTAL_NODECOUNT=%d"
        % (cfg.shard_consensus_size, cfg.meta_consensus_size, cfg.total_node_count),
        "resolved: SUPERNOVA_ROUND=%d" % cfg.supernova_round,
        "resolved: ROUNDS_PER_EPOCH=%d" % cfg.rounds_per_epoch,
        "resolved: USE_PROXY=1 PROXY_USE_VALIDATORS=1 LOGLEVEL=%s" % cfg.loglevel,
        "resolved: TESTNETDIR=%s PORT_PROXY=%d PORT_SEEDNODE=%d"
        % (cfg.testnet_dir, cfg.proxy_port, cfg.seednode_port),
        "resolved: MX_CHAIN_GO_DIR=%s" % cfg.repo_root,
        "resolved: validator_p2p_ports=%d..%d"
        % (cfg.validator_port_origin, cfg.validator_port_origin + last),
        "resolved: validator_rest_ports=%d..%d"
        % (cfg.validator_rest_origin, cfg.validator_rest_origin + last),
        "resolved: logs=%s pids=%s"
        % (cfg.log_dir, cfg.pid_dir),
        "resolved: TXGEN port=%d accounts=%d scenarios=[%s] bulk=%s size=%d interval_ms=%d dir=%s"
        % (
            cfg.txgen_port,
            cfg.txgen_num_accounts,
            ",".join(cfg.txgen_scenarios),
            "on" if cfg.txgen_bulk_enabled else "off",
            cfg.txgen_bulk_size,
            cfg.txgen_bulk_interval_ms,
            cfg.txgen_dir,
        ),
    ]
    return "\n".join(lines)
