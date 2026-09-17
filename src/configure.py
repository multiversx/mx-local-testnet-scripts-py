#!/usr/bin/env python3
"""Pure file-transformation helpers (no I/O, no subprocesses).

These mirror the ``sed``/``awk`` edits that ``mac/start.sh`` performs on the
generated configuration files, in idiomatic Python operating on strings so
they are easy to unit test.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

_MAX_NODES_ENTRY_RE = re.compile(
    r"\{\s*EpochEnable\s*=\s*(\d+)\s*,\s*"
    r"MaxNumNodes\s*=\s*(\d+)\s*,\s*"
    r"NodesToShufflePerShard\s*=\s*(\d+)\s*\}"
)
_STEP3_EPOCH_RE = re.compile(r"^\s*StakingV4Step3EnableEpoch\s*=\s*(\d+)\s*$")


def set_toml_value(text: str, key: str, value: str) -> str:
    """Replace ``key = ...`` with ``key = <value>`` on non-comment lines."""

    def replace(match: re.Match) -> str:
        return "%s%s" % (match.group(1), value)

    pattern = re.compile(
        r"^(?!\s*#)(\s*%s\s*=\s*).*$" % re.escape(key), re.MULTILINE
    )
    return pattern.sub(replace, text)


def set_json_value(text: str, key: str, formatted_value: str) -> str:
    """Replace ``"key": ...`` with ``"key": <formatted_value>,``."""
    pattern = re.compile(
        r'^(\s*"%s"\s*:\s*).*$' % re.escape(key), re.MULTILINE
    )

    def replace(match: re.Match) -> str:
        return "%s%s," % (match.group(1), formatted_value)

    return pattern.sub(replace, text)


def _sub_first(text: str, pattern: str, replacement: str) -> str:
    """Replace the first match per line (mirrors ``sed s///`` without ``g``)."""
    return "\n".join(
        re.sub(pattern, replacement, line, count=1)
        for line in text.split("\n")
    )


def _replace_all(text: str, old: str, new: str) -> Tuple[str, bool]:
    """Replace every occurrence of ``old`` with ``new``.

    Returns ``(text, applied)`` where *applied* is ``True`` when at least
    one replacement was made.  The caller uses this to warn when an
    expected literal (e.g. ``RoundDuration = 6000``) disappeared from the
    upstream config file.
    """
    if old not in text:
        return text, False
    return text.replace(old, new), True


def apply_chain_params(
    text: str,
    shard_consensus: int,
    meta_consensus: int,
    round_duration_ms: int,
    hysteresis: float,
    rounds_per_epoch: int,
) -> Tuple[str, List[str]]:
    """Apply topology-dependent values to a node ``config.toml``.

    Returns ``(text, warnings)`` where *warnings* lists any expected
    literal markers (e.g. ``RoundDuration = 6000``) that were absent from
    the file — a strong signal that upstream config defaults changed and
    the testnet may not behave as intended.
    """
    warnings: List[str] = []
    text = _sub_first(
        text,
        r"ShardConsensusGroupSize[^,]*",
        "ShardConsensusGroupSize = %d" % shard_consensus,
    )
    text = _sub_first(
        text,
        r"ShardMinNumNodes[^,]*",
        "ShardMinNumNodes = %d" % shard_consensus,
    )
    text = _sub_first(
        text,
        r"MetachainConsensusGroupSize[^,]*",
        "MetachainConsensusGroupSize = %d" % meta_consensus,
    )
    text = _sub_first(
        text,
        r"MetachainMinNumNodes[^,]*",
        "MetachainMinNumNodes = %d" % meta_consensus,
    )
    text, applied = _replace_all(
        text, "RoundDuration = 6000", "RoundDuration = %d" % round_duration_ms)
    if not applied:
        warnings.append(
            "'RoundDuration = 6000' not found in config.toml; "
            "round duration not applied (possible upstream config drift)")
    text = _sub_first(text, r"Hysteresis[^,]*", "Hysteresis = %s" % hysteresis)
    if rounds_per_epoch != 0:
        scaled = rounds_per_epoch * round_duration_ms // 600
        text, applied = _replace_all(
            text, "RoundsPerEpoch = 2000", "RoundsPerEpoch = %d" % scaled)
        if not applied:
            warnings.append(
                "'RoundsPerEpoch = 2000' not found in config.toml; "
                "fast-epochs scaling not applied (possible upstream config drift)")
        text, applied = _replace_all(
            text, "RoundsPerEpoch = 200", "RoundsPerEpoch = %d" % rounds_per_epoch)
        if not applied:
            warnings.append(
                "'RoundsPerEpoch = 200' not found in config.toml; "
                "fast-epochs override not applied (possible upstream config drift)")
        text, applied = _replace_all(
            text, "MinRoundsBetweenEpochs = 20",
            "MinRoundsBetweenEpochs = %d" % rounds_per_epoch)
        if not applied:
            warnings.append(
                "'MinRoundsBetweenEpochs = 20' not found in config.toml; "
                "fast-epochs override not applied (possible upstream config drift)")
    return text, warnings


def empty_cpu_flags(text: str) -> str:
    """Empty the ``CPUFlags`` list (Apple Silicon has no x86 SSE flags)."""
    return re.sub(r"(?m)^(\s*CPUFlags\s*=\s*).*$", r"\g<1>[]", text)


def apply_supernova_round(text: str, supernova_round: int) -> Tuple[str, List[str]]:
    """Rewrite every round-keyed Supernova entry in a node ``config.toml``.

    ``SupernovaEnableRound`` (``enableRounds.toml``) plus the ``Versions``,
    ``ProcessConfigsByRound``, ``EpochStartConfigsByRound``,
    ``ConsensusConfigsByRound`` and ``Antiflood`` entries must all carry the
    same round, otherwise the node refuses to start
    (``ErrSupernovaActivationConfigMismatch``). The bare ``Round`` pattern is
    line-anchored so ``StartRound`` lines and unrelated numbers (e.g. a
    ``SizeInBytes`` value containing ``440``) are left untouched.

    Returns ``(text, warnings)`` where *warnings* list any expected literal
    markers that were absent from the file — a strong signal that the
    upstream config changed and the round value was not rewritten.
    """
    warnings: List[str] = []
    if supernova_round == 440:
        return text, warnings
    before = text
    text = text.replace(
        "EnableRound = 440", "EnableRound = %d" % supernova_round
    )
    text = text.replace(
        "StartRound = 440", "StartRound = %d" % supernova_round
    )
    text = re.sub(
        r"(?m)^(\s*)Round = 440\s*$",
        lambda match: "%sRound = %d" % (match.group(1), supernova_round),
        text,
    )
    if text == before:
        warnings.append(
            "no Supernova round markers ('= 440') found in config.toml; "
            "supernova round %d not applied (possible upstream config drift)"
            % supernova_round
        )
    return text, warnings


def apply_supernova_enable_round(text: str, supernova_round: int) -> str:
    """Rewrite ``SupernovaEnableRound`` in an ``enableRounds.toml``."""
    return text.replace(
        'Round = "440"', 'Round = "%d"' % supernova_round
    )


def max_nodes_entries(text: str) -> List[Tuple[int, int, int]]:
    """Parse ``MaxNodesChangeEnableEpoch`` entries as (epoch, max, shuffle)."""
    block_start = text.find("MaxNodesChangeEnableEpoch")
    block = text[block_start:] if block_start != -1 else text
    return [
        (int(epoch), int(max_nodes), int(shuffle))
        for epoch, max_nodes, shuffle in _MAX_NODES_ENTRY_RE.findall(block)
    ]


def staking_v4_step3_epoch(text: str) -> Optional[int]:
    """Return the ``StakingV4Step3EnableEpoch`` value, if present."""
    for line in text.splitlines():
        match = _STEP3_EPOCH_RE.match(line)
        if match:
            return int(match.group(1))
    return None


def update_staking_v4_max_nodes(text: str, shard_count: int) -> Tuple[str, str]:
    """Recompute ``MaxNumNodes`` for ``StakingV4Step3EnableEpoch``.

    The node enforces (see ``config/configChecker.go``)::

        MaxNumNodes[step3] = MaxNumNodes[prev] - (shards + 1) * shuffle

    Returns the updated text plus a human-readable message describing what
    happened (mirrors the shell helper's output, including its edge cases).
    """
    entries = max_nodes_entries(text)
    if len(entries) < 2:
        return text, "Not enough entries found to update"

    step3 = staking_v4_step3_epoch(text)
    index = next(
        (pos for pos, entry in enumerate(entries) if entry[0] == step3), None
    )
    if step3 is None or index is None or index == 0:
        return (
            text,
            "Warning: MaxNodesChangeEnableEpoch does not contain an entry "
            "enable epoch for StakingV4Step3EnableEpoch, nodes might fail "
            "to start...",
        )

    _, prev_max, shuffle = entries[index - 1]
    new_max = prev_max - (shard_count + 1) * shuffle

    def replace(match: re.Match) -> str:
        return match.group(0).replace(
            "MaxNumNodes = %d" % entries[index][1],
            "MaxNumNodes = %d" % new_max,
            1,
        )

    pattern = re.compile(
        r"\{\s*EpochEnable\s*=\s*%d\s*,\s*MaxNumNodes\s*=\s*\d+\s*,"
        r"\s*NodesToShufflePerShard\s*=\s*\d+\s*\}" % step3
    )
    updated, count = pattern.subn(replace, text, count=1)
    if count == 0:
        return text, "Not enough entries found to update"
    old_entry = "EpochEnable = %d, MaxNumNodes = %d" % (step3, entries[index][1])
    new_entry = "EpochEnable = %d, MaxNumNodes = %d" % (step3, new_max)
    return updated, "Updating entry in MaxNodesChangeEnableEpoch from %s to %s" % (
        old_entry,
        new_entry,
    )


def p2p_pubkey_from_first_line(first_line: str) -> str:
    """Extract the libp2p pubkey from a ``BEGIN PRIVATE KEY for ...`` line."""
    match = re.search(r"for\s+([A-Za-z0-9]+)-*\s*$", first_line.strip())
    if not match:
        raise ValueError("cannot parse p2p public key from %r" % first_line)
    return match.group(1)


def truncate_proxy_observers(text: str) -> str:
    """Drop the ``[[Observers]]`` blocks from a proxy ``config.toml``."""
    lines = text.splitlines(keepends=True)
    kept = []
    for line in lines:
        if line.strip() == "[[Observers]]":
            break
        kept.append(line)
    result = "".join(kept)
    if result and not result.endswith("\n"):
        result += "\n"
    return result


def _proxy_observer_block(shard_id: int, rest_port: int) -> str:
    return (
        "[[Observers]]\n"
        "   ShardId = %d\n" % shard_id
        + '   Address = "http://127.0.0.1:%d"\n' % rest_port
        + "\n"
    )


def render_proxy_observers(slots, rest_origin: int) -> str:
    """Render one ``[[Observers]]`` block per validator REST API.

    ``slots`` is a ``validator_slots()`` list of
    ``(global_index, kind, shard_id)``. Shard entries come first: the
    proxy asks the observers IN FILE ORDER for the shard count and a
    metachain node answers that query with ``1``, which would abort the
    proxy startup on multi-shard testnets. Correct ports follow from the
    slots (metachain indices first).
    """
    blocks = []
    shards = sorted({shard for _, kind, shard in slots if kind == "shard"})
    for shard in shards:
        for index, kind, shard_id in slots:
            if kind == "shard" and shard_id == shard:
                blocks.append(_proxy_observer_block(shard_id, rest_origin + index))
    for index, kind, shard_id in slots:
        if kind == "meta":
            blocks.append(_proxy_observer_block(shard_id, rest_origin + index))
    return "".join(blocks)


def parse_txgen_scenarios(raw: str) -> List[str]:
    """Parse a txgen ``Scenarios`` value into a list of scenario names.

    Accepts the legacy full line (``Scenarios = ["basic", "erc20"]``),
    a bare TOML list (``["basic", "esdt"]``) or a plain comma-separated
    list (``basic,erc20,esdt``).
    """
    quoted = re.findall(r'"([^"]+)"', raw)
    if quoted:
        return [name.strip() for name in quoted if name.strip()]
    single_quoted = re.findall(r"'([^']+)'", raw)
    if single_quoted:
        return [name.strip() for name in single_quoted if name.strip()]
    return [part.strip() for part in raw.split(",") if part.strip()]


def render_txgen_scenarios(scenarios: List[str]) -> str:
    """Render the ``Scenarios = [...]`` line for a txgen ``config.toml``."""
    return "Scenarios = [%s]" % ", ".join('"%s"' % name for name in scenarios)


def apply_txgen_config(
    text: str, server_port: int, proxy_port: int, scenarios: List[str]
) -> str:
    """Point a txgen ``config.toml`` at the local testnet proxy.

    Mirrors ``updateTxGenConfig`` in ``include/config.sh``: the txgen REST
    port, the proxy URL and the active scenario list.
    """
    text = set_toml_value(text, "ServerPort", str(server_port))
    text = set_toml_value(
        text, "ProxyServerURL", '"http://127.0.0.1:%d"' % proxy_port
    )
    pattern = re.compile(
        r"^(?!\s*#)(\s*Scenarios\s*=\s*).*$", re.MULTILINE
    )

    # Rebuild the whole line, keeping indentation.
    def replace_line(match: re.Match) -> str:
        indent = re.match(r"^(\s*)", match.group(0)).group(1)
        return "%s%s" % (indent, render_txgen_scenarios(scenarios))

    return pattern.sub(replace_line, text)


def enable_db_lookup_extension(text: str) -> str:
    """Set ``Enabled = true`` inside the ``[DbLookupExtensions]`` section.

    Mirrors ``--operation-mode db-lookup-extension`` (which additionally
    forces ``StoragePruning.Enabled``, already true in our configs): nodes
    index the miniblock/tx-hash lookups backing the proxy transaction
    endpoints. Only the flag inside that section is touched — every other
    ``Enabled`` key in the file is left alone.
    """
    lines = text.splitlines(keepends=True)
    in_section = False
    done = False
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped == "[DbLookupExtensions]"
        if in_section and not done and not stripped.startswith("#"):
            if re.match(r"Enabled\s*=", stripped):
                indent = line[: len(line) - len(line.lstrip())]
                ending = "\n" if line.endswith("\n") else ""
                line = "%sEnabled = true%s" % (indent, ending)
                done = True
        out.append(line)
    return "".join(out)
