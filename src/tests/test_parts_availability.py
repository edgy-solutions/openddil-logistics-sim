"""
Tests for the spare-parts-availability stand-in (ADR-0046 §1
`picture.spare`): config validation + loading (config.py's
PartsAvailabilityConfig / _parse_parts_availability), the pure
spare_picture helper, and HqProducer.publish_parts_availability.

Stock is static configuration, not a simulation of consumption -- see
publisher.py's publish_parts_availability docstring. Label precedence
intentionally does NOT reuse ReleasabilityDeclaration's site_nation
fallback: a site with no nation configured stays unlabelled
(originator_nation: null), never defaulted.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from logistics_sim.config import (
    PartsAvailabilityPart,
    PartsAvailabilitySite,
    SimConfig,
    spare_picture,
)
from logistics_sim.publisher import HqProducer

# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------

_BASE_LINES = [
    "tick_interval_s: 30",
    "output_topic: asset-element-telemetry",
    "asset_profiles:",
    "  - name: mrad",
]


def _write_config(tmp_path: Path, parts_availability_lines: list[str]) -> Path:
    lines = list(_BASE_LINES)
    if parts_availability_lines:
        lines.append("parts_availability:")
        lines.extend(parts_availability_lines)
    f = tmp_path / "config.yaml"
    f.write_text("\n".join(lines) + "\n")
    return f


_VALID_PA_LINES = [
    "  topic: parts-availability",
    "  interval_s: 60",
    "  sites:",
    "    edge-01: {nation: ATL, nearest: [region-east, edge-02, hq]}",
    "    edge-02: {nation: BDR, nearest: [region-east, edge-01, hq]}",
    "    region-east: {nation: ATL, nearest: [edge-01, edge-02, hq]}",
    "    hq: {nation: ATL, nearest: [region-east, edge-01, edge-02]}",
    "  parts:",
    "    - part_ref: part:array-module",
    "      item: array module",
    "      on_hand: {edge-01: 0, edge-02: 1, region-east: 2, hq: 4}",
]


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

def test_default_config_loads(tmp_path: Path) -> None:
    """The repo's own config/default.yaml loads cleanly end to end."""
    repo_root = Path(__file__).resolve().parents[2]
    cfg = SimConfig.load(repo_root / "config" / "default.yaml")
    pa = cfg.parts_availability
    assert pa.topic == "parts-availability"
    assert pa.interval_s == 60
    assert set(pa.sites) == {"edge-01", "edge-02", "region-east", "hq"}
    assert pa.parts[0].part_ref == "part:array-module"


def test_valid_parts_availability_loads(tmp_path: Path) -> None:
    path = _write_config(tmp_path, _VALID_PA_LINES)
    cfg = SimConfig.load(path)
    pa = cfg.parts_availability
    assert pa.sites["edge-01"] == PartsAvailabilitySite(
        nation="ATL", nearest=("region-east", "edge-02", "hq"),
    )
    assert pa.parts == (
        PartsAvailabilityPart(
            part_ref="part:array-module",
            item="array module",
            on_hand={"edge-01": 0, "edge-02": 1, "region-east": 2, "hq": 4},
        ),
    )


def test_unknown_site_in_nearest_refused(tmp_path: Path) -> None:
    lines = [
        "  sites:",
        "    edge-01: {nation: ATL, nearest: [nowhere]}",
        "  parts: []",
    ]
    path = _write_config(tmp_path, lines)
    with pytest.raises(ValueError, match="unknown site"):
        SimConfig.load(path)


def test_unknown_site_in_on_hand_refused(tmp_path: Path) -> None:
    lines = [
        "  sites:",
        "    edge-01: {nation: ATL, nearest: []}",
        "  parts:",
        "    - part_ref: part:array-module",
        "      item: array module",
        "      on_hand: {nowhere: 1}",
    ]
    path = _write_config(tmp_path, lines)
    with pytest.raises(ValueError, match="unknown site"):
        SimConfig.load(path)


def test_negative_on_hand_refused(tmp_path: Path) -> None:
    lines = [
        "  sites:",
        "    edge-01: {nation: ATL, nearest: []}",
        "  parts:",
        "    - part_ref: part:array-module",
        "      item: array module",
        "      on_hand: {edge-01: -1}",
    ]
    path = _write_config(tmp_path, lines)
    with pytest.raises(ValueError, match="negative"):
        SimConfig.load(path)


def test_duplicate_part_ref_refused(tmp_path: Path) -> None:
    lines = [
        "  sites:",
        "    edge-01: {nation: ATL, nearest: []}",
        "  parts:",
        "    - part_ref: part:array-module",
        "      item: array module",
        "      on_hand: {edge-01: 1}",
        "    - part_ref: part:array-module",
        "      item: array module (dup)",
        "      on_hand: {edge-01: 2}",
    ]
    path = _write_config(tmp_path, lines)
    with pytest.raises(ValueError, match="duplicate"):
        SimConfig.load(path)


# ---------------------------------------------------------------------------
# spare_picture (pure helper)
# ---------------------------------------------------------------------------

def _sites() -> dict[str, PartsAvailabilitySite]:
    return {
        "edge-01": PartsAvailabilitySite("ATL", ("region-east", "edge-02", "hq")),
        "edge-02": PartsAvailabilitySite("BDR", ("region-east", "edge-01", "hq")),
        "region-east": PartsAvailabilitySite("ATL", ("edge-01", "edge-02", "hq")),
        "hq": PartsAvailabilitySite("ATL", ("region-east", "edge-01", "edge-02")),
    }


def _availability() -> dict[str, dict[str, int]]:
    return {
        "part:array-module": {
            "edge-01": 0, "edge-02": 1, "region-east": 2, "hq": 4,
        },
    }


def test_spare_picture_finds_nearest_with_stock() -> None:
    result = spare_picture(
        "edge-01", "part:array-module", _availability(), _sites(),
    )
    assert result == {
        "on_hand_here": 0,
        "nearest_site_with_stock": "region-east",
        "nearest_on_hand": 2,
    }


def test_spare_picture_no_stock_anywhere_is_none() -> None:
    availability = {"part:array-module": {s: 0 for s in _sites()}}
    result = spare_picture(
        "edge-01", "part:array-module", availability, _sites(),
    )
    assert result["nearest_site_with_stock"] is None
    assert result["nearest_on_hand"] == 0
    assert result["on_hand_here"] == 0


def test_spare_picture_site_itself_stocked_nearest_is_other_site() -> None:
    result = spare_picture(
        "hq", "part:array-module", _availability(), _sites(),
    )
    assert result["on_hand_here"] == 4
    assert result["on_hand_here"] > 0
    # hq's own stock must not be reported as its own "nearest" answer --
    # nearest is the first OTHER site (hq's nearest ring never lists hq).
    assert result["nearest_site_with_stock"] == "region-east"
    assert result["nearest_on_hand"] == 2


# ---------------------------------------------------------------------------
# HqProducer.publish_parts_availability
# ---------------------------------------------------------------------------

class _StubProducer:
    """Minimal stand-in for AIOKafkaProducer.send_and_wait -- records
    every (topic, value, key) call so tests can decode the JSON. Same
    pattern as test_releasability.py's _StubProducer."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def send_and_wait(self, topic, value, key=None) -> None:
        self.sent.append((topic, value, key))


def _make_producer() -> tuple[HqProducer, _StubProducer]:
    producer = HqProducer("brokers:9092", "asset-element-telemetry")
    stub = _StubProducer()
    producer._producer = stub  # type: ignore[attr-defined]
    return producer, stub


def _cfg_from_yaml(tmp_path: Path):
    path = _write_config(tmp_path, _VALID_PA_LINES)
    return SimConfig.load(path).parts_availability


@pytest.mark.asyncio
async def test_publish_one_record_per_site_and_part(tmp_path: Path) -> None:
    cfg = _cfg_from_yaml(tmp_path)
    producer, stub = _make_producer()
    published = await producer.publish_parts_availability(cfg)
    assert published == 4
    assert len(stub.sent) == 4

    keys = sorted(key.decode("utf-8") for _, _, key in stub.sent)
    assert keys == [
        "edge-01:part:array-module",
        "edge-02:part:array-module",
        "hq:part:array-module",
        "region-east:part:array-module",
    ]

    envelopes = {
        json.loads(value)["site"]: json.loads(value) for _, value, _ in stub.sent
    }
    assert envelopes["edge-01"]["originator_nation"] == "ATL"
    assert envelopes["edge-02"]["originator_nation"] == "BDR"
    assert envelopes["hq"]["originator_nation"] == "ATL"
    assert envelopes["region-east"]["originator_nation"] == "ATL"
    for env in envelopes.values():
        assert env["releasable_to"] == []
        assert env["source"] == "stand-in"
        assert env["part_ref"] == "part:array-module"
        assert env["item"] == "array module"
        assert isinstance(env["as_of"], int)
    assert envelopes["edge-01"]["on_hand"] == 0
    assert envelopes["edge-02"]["on_hand"] == 1
    assert envelopes["region-east"]["on_hand"] == 2
    assert envelopes["hq"]["on_hand"] == 4
    assert all(topic == cfg.topic for topic, _, _ in stub.sent)


@pytest.mark.asyncio
async def test_publish_unlabelled_site_is_null_not_omitted(tmp_path: Path) -> None:
    """A site with no nation configured publishes originator_nation:
    null / releasable_to: [] -- the fields are PRESENT with those
    values, not omitted (unlike the per-asset telemetry envelopes in
    publisher.py, which omit the keys entirely for an unlabelled
    asset)."""
    lines = [
        "  sites:",
        "    edge-01: {nation: ATL, nearest: []}",
        "    edge-03: {nearest: []}",  # no nation configured
        "  parts:",
        "    - part_ref: part:array-module",
        "      item: array module",
        "      on_hand: {edge-01: 1, edge-03: 0}",
    ]
    path = _write_config(tmp_path, lines)
    cfg = SimConfig.load(path).parts_availability
    producer, stub = _make_producer()
    published = await producer.publish_parts_availability(cfg)
    assert published == 2

    envelopes = {
        json.loads(value)["site"]: json.loads(value) for _, value, _ in stub.sent
    }
    assert envelopes["edge-03"]["originator_nation"] is None
    assert envelopes["edge-03"]["releasable_to"] == []
    assert "originator_nation" in envelopes["edge-03"]
    assert "releasable_to" in envelopes["edge-03"]
    assert envelopes["edge-01"]["originator_nation"] == "ATL"
