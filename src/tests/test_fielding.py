"""Tests for the fielding stand-in (Contract B fielding plane): config
validation (config.py's _parse_fielding), HqProducer.publish_fielding and
its edge-producer wiring, and the absent-block loop gate."""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from logistics_sim import main
from logistics_sim.config import FieldingConfig, FieldingRecord, SimConfig
from logistics_sim.publisher import HqProducer

_BASE = [
    "tick_interval_s: 30",
    "output_topic: asset-element-telemetry",
    "asset_profiles:",
    "  - name: mrad",
]

_ABSENT = object()

_REC = {
    "serial": "SN-2001", "platform_variant": "VARIANT_X", "unit": "unit-a",
    "site": "edge-01", "owning_nation": "ATL", "releasable_to": [],
    "due_by": "2026-10-22T00:00:00Z",
}


def _rec_lines(**over) -> list[str]:
    rec = {**_REC, **over}
    items = [(k, v) for k, v in rec.items() if v is not _ABSENT]
    out = []
    for i, (k, v) in enumerate(items):
        out.append(("    - " if i == 0 else "      ") + f"{k}: {json.dumps(v)}")
    return out


def _load(tmp_path: Path, record_lines: list[str] | None, extra=()) -> SimConfig:
    lines = list(_BASE)
    if record_lines is not None:
        lines.append("fielding:")
        lines.extend(extra)
        lines.append("  records:")
        lines.extend(record_lines)
    f = tmp_path / "c.yaml"
    f.write_text("\n".join(lines) + "\n")
    return SimConfig.load(f)


def test_absent_block_is_empty(tmp_path: Path) -> None:
    cfg = _load(tmp_path, None)
    assert cfg.fielding.records == ()


def test_defaults(tmp_path: Path) -> None:
    f = _load(tmp_path, _rec_lines()).fielding
    assert (f.topic, f.interval_s) == ("asset-fielding", 60)
    r = f.records[0]
    assert r.releasable_to == () and r.site == "edge-01"
    assert r.due_by_ns == 1792627200_000000000


def test_releasable_to_defaults_to_empty(tmp_path: Path) -> None:
    lines = _rec_lines(releasable_to=_ABSENT)
    assert _load(tmp_path, lines).fielding.records[0].releasable_to == ()


@pytest.mark.parametrize("over", [
    {"serial": ""}, {"serial": "none"}, {"serial": "None"},
    {"platform_variant": ""}, {"unit": ""},
    {"site": "edge-99"}, {"site": "hq"},
    {"owning_nation": ""},
    {"releasable_to": "BDR"}, {"releasable_to": [""]},
    {"releasable_to": ["ATL"]},
    {"due_by": "next tuesday"}, {"due_by": "2026-10-22T00:00:00"},
    {"due_by": ""},
])
def test_refused_at_load(tmp_path: Path, over) -> None:
    with pytest.raises(ValueError, match="fielding.records"):
        _load(tmp_path, _rec_lines(**over))


def test_duplicate_serial_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="duplicate serial"):
        _load(tmp_path, _rec_lines() + _rec_lines())


def test_interval_must_be_positive(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="interval_s"):
        _load(tmp_path, _rec_lines(), extra=["  interval_s: 0"])


# -- publisher ---------------------------------------------------------------

class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def send_and_wait(self, topic, value, key):
        self.sent.append((topic, value, key))

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass


class _Boom(_FakeKafka):
    async def send_and_wait(self, topic, value, key):
        raise RuntimeError("broker down")


def _cfg(*recs: FieldingRecord) -> FieldingConfig:
    return FieldingConfig(records=tuple(recs))


def _fr(serial="SN-2001", site="edge-01", **kw) -> FieldingRecord:
    base = dict(
        serial=serial, platform_variant="VARIANT_X", unit="unit-a",
        site=site, owning_nation="ATL", releasable_to=("BDR",),
        due_by_ns=1792627200_000000000,
    )
    base.update(kw)
    return FieldingRecord(**base)


def _producer():
    hq = _FakeKafka()
    edges = {"edge-01": _FakeKafka(), "edge-02": _FakeKafka()}
    p = HqProducer("hq:1", "asset-element-telemetry")
    p._producer = hq
    p._edge_producers = dict(edges)
    return p, hq, edges


async def test_record_shape_key_and_routing() -> None:
    p, hq, edges = _producer()
    n = await p.publish_fielding(_cfg(_fr(), _fr("SN-2002", "edge-02")))
    assert n == 2
    assert hq.sent == []
    (topic, value, key), = edges["edge-01"].sent
    assert topic == "asset-fielding" and key == b"SN-2001"
    d = json.loads(value)
    ns = d["observed_at_ns"]
    assert d == {
        "serial": "SN-2001", "platform_variant": "VARIANT_X",
        "unit": "unit-a", "site": "edge-01",
        "due_by_ns": 1792627200_000000000, "observed_at_ns": ns,
        "provenance": {"originator_nation": "ATL", "releasable_to": ["BDR"]},
        "extraction": {"cursor": f"sweep-{ns}", "extracted_at_ns": ns},
    }
    assert [k for _, _, k in edges["edge-02"].sent] == [b"SN-2002"]
    other = json.loads(edges["edge-02"].sent[0][1])
    assert other["observed_at_ns"] == ns  # one timestamp per sweep


async def test_send_failure_skips_record() -> None:
    p, hq, edges = _producer()
    p._edge_producers["edge-01"] = _Boom()
    n = await p.publish_fielding(_cfg(_fr(), _fr("SN-2002", "edge-02")))
    assert n == 1 and len(edges["edge-02"].sent) == 1


async def test_site_without_producer_is_skipped() -> None:
    p, hq, edges = _producer()
    assert await p.publish_fielding(_cfg(_fr(site="edge-09"))) == 0
    assert hq.sent == []


async def test_hq_mode_start_creates_fielding_edge_producer(monkeypatch) -> None:
    made: list[str] = []

    def _new(self, brokers):
        made.append(brokers)
        return _FakeKafka()

    monkeypatch.setattr(HqProducer, "_new_kafka_producer", _new)
    p = HqProducer(
        "hq:1", "asset-element-telemetry",
        fielding_edges={"edge-01": "edge-01:9092"},
    )
    await p.start()
    assert not p.edge_mode
    assert made == ["hq:1", "edge-01:9092"]
    assert set(p._edge_producers) == {"edge-01"}
    assert await p.publish_fielding(_cfg(_fr())) == 1
    assert len(p._edge_producers["edge-01"].sent) == 1
    assert p._producer.sent == []
    await p.stop()


async def test_hq_mode_without_fielding_makes_no_edge_producers(monkeypatch) -> None:
    monkeypatch.setattr(
        HqProducer, "_new_kafka_producer", lambda self, b: _FakeKafka())
    p = HqProducer("hq:1", "asset-element-telemetry")
    await p.start()
    assert p._edge_producers == {}


def test_loop_gate_is_on_records(tmp_path: Path) -> None:
    # main starts _fielding_loop only `if cfg.fielding.records`.
    assert not _load(tmp_path, None).fielding.records
    assert _load(tmp_path, _rec_lines()).fielding.records
    assert "if cfg.fielding.records" in inspect.getsource(main._serve)
