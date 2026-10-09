"""
Tests for owning-edge element-snapshot routing (element_publish_tier).

Fake producers stand in for the brokers: the router is exercised
without any Kafka connection.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from logistics_sim.asset_discovery import AssetRoster
from logistics_sim.config import SimConfig
from logistics_sim.element_gen import AssetState
from logistics_sim.publisher import HqProducer


def _state() -> AssetState:
    return AssetState(
        platform_variant="MRAD_Sensor",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=True,
        actively_receiving=True,
    )


class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def send_and_wait(self, topic, value, key):
        self.sent.append((topic, value, key))


def _producer(edge_mode: bool, owners: dict[str, str]):
    hq = _FakeKafka()
    edges = {"edge-a": _FakeKafka(), "edge-b": _FakeKafka()}
    p = HqProducer(
        "hq:1", "asset-element-telemetry",
        edge_brokers={k: k for k in edges} if edge_mode else None,
        edge_of=owners.get if edge_mode else None,
    )
    p._producer = hq
    p._edge_producers = dict(edges) if edge_mode else {}
    return p, hq, edges


async def _publish(p: HqProducer, asset_id: str) -> None:
    await p.publish_snapshot(asset_id, _state(), "mrad", [], False)


def _normalize(value: bytes) -> dict:
    import json
    d = json.loads(value)
    d.pop("observed_at_ns")
    return d


# 1. roster ---------------------------------------------------------

def test_roster_edge_of_last_report_wins_and_none_keeps() -> None:
    r = AssetRoster()
    assert r.edge_of("a1") is None
    r.upsert("a1", _state(), "edge-a")
    assert r.edge_of("a1") == "edge-a"
    r.upsert("a1", _state(), "edge-b")
    assert r.edge_of("a1") == "edge-b"
    r.upsert("a1", _state())
    assert r.edge_of("a1") == "edge-b"


# 2. edge mode routing ----------------------------------------------

@pytest.mark.asyncio
async def test_edge_mode_routes_to_owning_edge_only() -> None:
    p, hq, edges = _producer(True, {"a1": "edge-a"})
    await _publish(p, "a1")
    assert hq.sent == []
    assert edges["edge-b"].sent == []
    assert len(edges["edge-a"].sent) == 1
    topic, value, key = edges["edge-a"].sent[0]
    assert topic == "asset-element-telemetry" and key == b"a1"

    hp, hq2, _ = _producer(False, {})
    await _publish(hp, "a1")
    assert _normalize(value) == _normalize(hq2.sent[0][1])


# 3. unknown owner --------------------------------------------------

@pytest.mark.asyncio
async def test_edge_mode_unknown_owner_skips_and_warns_once(caplog) -> None:
    p, hq, edges = _producer(True, {"a2": "edge-zzz"})
    with caplog.at_level(logging.WARNING, logger="logistics_sim.publisher"):
        await _publish(p, "a1")
        await _publish(p, "a1")
        await _publish(p, "a2")  # owner without a producer
    assert hq.sent == []
    assert all(e.sent == [] for e in edges.values())
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 2  # one per asset, not per call
    assert "a1" in warned[0].getMessage() and "a2" in warned[1].getMessage()


# 4. hq mode --------------------------------------------------------

@pytest.mark.asyncio
async def test_hq_mode_publishes_to_hq() -> None:
    p, hq, edges = _producer(False, {})
    await _publish(p, "a1")
    assert len(hq.sent) == 1
    assert edges["edge-a"].sent == [] and edges["edge-b"].sent == []


# 5. config ---------------------------------------------------------

def _cfg(tmp_path: Path, tier_line: str) -> Path:
    f = tmp_path / "c.yaml"
    f.write_text(
        "tick_interval_s: 30\n"
        + tier_line
        + "asset_profiles:\n"
        "  - name: mrad\n"
        "    matches_platform_variants: [\"MRAD_Sensor\"]\n"
        "    layers:\n"
        "      - name: RADAR UNIT\n"
        "        prefix: TR\n"
        "    faces:\n"
        "      - name: PRIMARY APERTURE\n"
        "        cols: 1\n"
        "        rows: 1\n"
    )
    return f


def test_config_tier_values(tmp_path: Path) -> None:
    assert SimConfig.load(_cfg(tmp_path, "")).element_publish_tier == "hq"
    assert SimConfig.load(
        _cfg(tmp_path, "element_publish_tier: edge\n")
    ).element_publish_tier == "edge"
    with pytest.raises(ValueError, match="element_publish_tier"):
        SimConfig.load(_cfg(tmp_path, "element_publish_tier: bogus\n"))
