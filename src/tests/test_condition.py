"""
Resolved condition drives synthesis, with provenance.

Locks in:
  * Discovery carries the Condition (proto and JSON paths); absent -> None.
  * The tick loop wakes on a condition level / moved_by change, never on an
    observed_at-only change.
  * synthesis_tier: a non-nominal condition overrides the legacy tier; a
    nominal or absent one falls back to it.
  * generate_snapshot per condition tier, and moved_by only on elements the
    condition lifted or silenced.
  * The envelope carries operational.condition and drops null moved_by.
  * The three new synthesis knobs default and override.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from logistics_sim.asset_discovery import AssetRoster, _extract
from logistics_sim.config import FaceSpec, LayerSpec, SimConfig, SynthesisKnobs
from logistics_sim.element_gen import (
    AssetState,
    ElementTelemetry,
    SeverityTier,
    generate_snapshot,
)
from logistics_sim.publisher import HqProducer

DEGRADED_POWER = ("POWER_STATE_OFF", "POWER_STATE_SHUTTING_DOWN", "POWER_STATE_MAINTENANCE")
DEGRADED_HEALTH = ("HEALTH_STATE_DEGRADED", "HEALTH_STATE_FAULT", "HEALTH_STATE_FAILED")

LAYERS = (
    LayerSpec(name="RADAR UNIT", prefix="TR", cols=0, rows=0),
    LayerSpec(name="BACKPLANE", prefix="BOARD", cols=2, rows=2),
)
FACES = (FaceSpec(name="PRIMARY APERTURE", cols=10, rows=10),)


def _knobs() -> SynthesisKnobs:
    return SynthesisKnobs(
        health_nominal_min=0.55, health_nominal_max=0.85,
        degraded_yellow_fraction=0.15, degraded_red_fraction=0.00,
        fault_yellow_fraction=0.20, fault_red_fraction=0.05,
        failed_yellow_fraction=0.30, failed_red_fraction=0.20,
        degraded_fraction=0.15,
        temp_min=30, temp_max=75, load_min=5, load_max=95,
        tick_drift_temp=1.5, tick_drift_load=5.0,
    )


def _cond(level: str, moved_by=("CONDITION_SOURCE_EMISSION",), observed_at="2026-10-08T00:00:00Z"):
    return {
        "level": f"CONDITION_LEVEL_{level}",
        "moved_by": list(moved_by),
        "claims": [{
            "source": moved_by[0] if moved_by else "CONDITION_SOURCE_EMISSION",
            "level": f"CONDITION_LEVEL_{level}",
            "detail": "x",
            "observed_at": observed_at,
        }],
    }


def _state(condition=None, **kw) -> AssetState:
    base = dict(
        platform_variant="MRAD_Sensor",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=True,
        actively_receiving=True,
        condition=condition,
    )
    base.update(kw)
    return AssetState(**base)


def _snap(state: AssetState, knobs: SynthesisKnobs | None = None):
    return generate_snapshot(
        asset_id="demo:cond-001", asset_state=state, layers=LAYERS, faces=FACES,
        synthesis=knobs or _knobs(), tick_bucket=7,
        degraded_power_states=DEGRADED_POWER, degraded_health_states=DEGRADED_HEALTH,
    )


# 1. discovery ------------------------------------------------------

def test_json_path_carries_condition() -> None:
    cond = _cond("DEGRADED")
    raw = json.dumps({
        "asset": {"asset_id": "a", "platform_variant": "V"},
        "operational_state": {"health_state": "HEALTH_STATE_NOMINAL", "condition": cond},
    }).encode()
    extracted = _extract(raw)
    assert extracted is not None and extracted[2].condition == cond


@pytest.mark.parametrize("bad", [None, "CONDITION_LEVEL_DEGRADED", {"moved_by": []}, {"level": 3}])
def test_json_path_condition_absent_or_malformed_is_none(bad) -> None:
    op = {"health_state": "HEALTH_STATE_NOMINAL"}
    if bad is not None:
        op["condition"] = bad
    raw = json.dumps({"asset": {"asset_id": "a", "platform_variant": "V"},
                      "operational_state": op}).encode()
    extracted = _extract(raw)
    assert extracted is not None and extracted[2].condition is None


def test_proto_path_carries_condition() -> None:
    pb = pytest.importorskip("openddil.telemetry.v1.telemetry_pb2")
    if "condition" not in pb.OperationalState.DESCRIPTOR.fields_by_name:
        pytest.skip("bindings predate OperationalState.condition")
    ev = pb.EntityTelemetryEvent()
    ev.asset.asset_id = "a"
    ev.asset.platform_variant = "V"
    c = ev.operational_state.condition
    c.level = pb.CONDITION_LEVEL_DEGRADED
    c.moved_by.append(pb.CONDITION_SOURCE_EMISSION)
    claim = c.claims.add()
    claim.source = pb.CONDITION_SOURCE_EMISSION
    claim.level = pb.CONDITION_LEVEL_DEGRADED
    claim.detail = "2/4 beams"
    claim.observed_at.FromJsonString("2026-10-08T00:00:00Z")
    extracted = _extract(ev.SerializeToString())
    assert extracted is not None
    got = extracted[2].condition
    assert got["level"] == "CONDITION_LEVEL_DEGRADED"
    assert got["moved_by"] == ["CONDITION_SOURCE_EMISSION"]
    assert got["claims"][0]["detail"] == "2/4 beams"
    assert got["claims"][0]["observed_at"] == "2026-10-08T00:00:00Z"


def test_proto_path_absent_condition_is_none() -> None:
    pb = pytest.importorskip("openddil.telemetry.v1.telemetry_pb2")
    ev = pb.EntityTelemetryEvent()
    ev.asset.asset_id = "a"
    ev.asset.platform_variant = "V"
    extracted = _extract(ev.SerializeToString())
    assert extracted is not None and extracted[2].condition is None


# 2. wake -----------------------------------------------------------

async def _wakes(old: AssetState, new: AssetState) -> bool:
    roster = AssetRoster()
    _ = roster.changed_event
    roster.upsert("a", old)
    roster.changed_event.clear()
    roster.upsert("a", new)
    return roster.changed_event.is_set()


@pytest.mark.asyncio
async def test_condition_level_change_wakes() -> None:
    assert await _wakes(_state(_cond("DEGRADED")), _state(_cond("CRITICAL")))


@pytest.mark.asyncio
async def test_condition_appearing_wakes() -> None:
    assert await _wakes(_state(None), _state(_cond("DEGRADED")))


@pytest.mark.asyncio
async def test_condition_moved_by_change_wakes() -> None:
    old = _state(_cond("DEGRADED", ("CONDITION_SOURCE_EMISSION",)))
    new = _state(_cond("DEGRADED", ("CONDITION_SOURCE_DATA_HEALTH",)))
    assert await _wakes(old, new)


@pytest.mark.asyncio
async def test_condition_observed_at_only_change_does_not_wake() -> None:
    old = _state(_cond("DEGRADED", observed_at="2026-10-08T00:00:00Z"))
    new = _state(_cond("DEGRADED", observed_at="2026-10-08T00:00:05Z"))
    assert not await _wakes(old, new)


# 3. tiers ----------------------------------------------------------

@pytest.mark.parametrize("level,tier", [
    ("DEGRADED", SeverityTier.DEGRADED),
    ("CRITICAL", SeverityTier.CRITICAL),
    ("NOT_EMITTING", SeverityTier.NOT_EMITTING),
    ("SENSOR_FAILED", SeverityTier.SENSOR_FAILED),
    ("DEACTIVATED", SeverityTier.DEACTIVATED),
    ("DESTROYED", SeverityTier.DESTROYED),
])
def test_condition_level_maps_to_tier(level, tier) -> None:
    s = _state(_cond(level))
    assert s.condition_tier() is tier
    assert s.synthesis_tier(DEGRADED_POWER, DEGRADED_HEALTH) is tier


@pytest.mark.parametrize("cond", [
    _cond("NOMINAL"), _cond("UNSPECIFIED"), {"level": "CONDITION_LEVEL_FUTURE"}, None,
])
def test_non_claiming_condition_has_no_tier(cond) -> None:
    assert _state(cond).condition_tier() is None
    assert _state(cond).moved_by_code() is None


def test_nominal_condition_falls_back_to_legacy_tier() -> None:
    s = _state(_cond("NOMINAL"), health_state="HEALTH_STATE_DEGRADED")
    assert s.synthesis_tier(DEGRADED_POWER, DEGRADED_HEALTH) is SeverityTier.DEGRADED


def test_no_condition_is_legacy_unchanged() -> None:
    for h, t in (("HEALTH_STATE_FAILED", SeverityTier.FAILED),
                 ("HEALTH_STATE_NOMINAL", SeverityTier.NOMINAL)):
        s = _state(None, health_state=h)
        assert s.synthesis_tier(DEGRADED_POWER, DEGRADED_HEALTH) is t
        assert s.severity_tier(DEGRADED_POWER, DEGRADED_HEALTH) is t


def test_moved_by_code_joins_short_names_in_order() -> None:
    s = _state(_cond("DEGRADED", ("CONDITION_SOURCE_DATA_HEALTH", "CONDITION_SOURCE_APPEARANCE_DAMAGE")))
    assert s.moved_by_code() == "data_health+appearance_damage"


# 4. generate_snapshot ----------------------------------------------

def _lifted(snap):
    return [e for e in snap if e.health > 0.90]


def test_degraded_condition_lifts_yellow_only_with_moved_by_on_lifted() -> None:
    snap = _snap(_state(_cond("DEGRADED")))
    assert not [e for e in snap if e.health > 0.97]
    yellow = [e for e in snap if 0.90 < e.health <= 0.97]
    assert yellow
    # exactly the lifted elements carry moved_by; all of them are lifted
    tagged = {e.element_id for e in snap if e.moved_by == "emission"}
    assert tagged
    assert all(e.moved_by is None for e in snap if e.element_id not in tagged)
    assert {e.element_id for e in snap if e.layer_depth == 0 and e.health > 0.90} <= tagged


def test_destroyed_condition_is_all_red_and_silent() -> None:
    snap = _snap(_state(_cond("DESTROYED")))
    assert all(e.health > 0.97 for e in snap)
    assert all(not e.tx_active and not e.rx_active for e in snap)
    assert all(e.moved_by == "emission" for e in snap)


def test_not_emitting_condition_lifts_nothing_and_silences_all() -> None:
    snap = _snap(_state(_cond("NOT_EMITTING", ("CONDITION_SOURCE_APPEARANCE_POWER",))))
    assert not _lifted(snap)
    assert all(not e.tx_active and not e.rx_active for e in snap)
    assert all(e.moved_by == "appearance_power" for e in snap)


def test_sensor_failed_silences_face_tx_only() -> None:
    snap = _snap(_state(_cond("SENSOR_FAILED")))
    faces = [e for e in snap if e.layer_depth == 0]
    inner = [e for e in snap if e.layer_depth > 0]
    assert faces and inner
    assert all(not e.tx_active and e.rx_active for e in faces)
    assert all(e.tx_active and e.rx_active for e in inner)
    assert all(e.moved_by == "emission" for e in faces)


def test_legacy_degraded_without_condition_sets_no_moved_by() -> None:
    snap = _snap(_state(None, health_state="HEALTH_STATE_DEGRADED"))
    assert _lifted(snap)
    assert all(e.moved_by is None for e in snap)


def test_nominal_condition_with_legacy_degraded_sets_no_moved_by() -> None:
    snap = _snap(_state(_cond("NOMINAL"), health_state="HEALTH_STATE_DEGRADED"))
    assert _lifted(snap)
    assert all(e.moved_by is None for e in snap)


def test_condition_degraded_matches_legacy_degraded_health_values() -> None:
    """Same seed, same tier: the roll is always drawn, so the condition path
    reproduces the legacy DEGRADED health values exactly."""
    a = _snap(_state(_cond("DEGRADED")))
    b = _snap(_state(None, health_state="HEALTH_STATE_DEGRADED"))
    assert [e.health for e in a] == [e.health for e in b]


def test_critical_uses_its_knobs() -> None:
    snap = _snap(_state(_cond("CRITICAL")))
    assert [e for e in snap if e.health > 0.97]
    assert all(e.tx_active and e.rx_active for e in snap)


# 5. publisher ------------------------------------------------------

class _FakeKafka:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def send_and_wait(self, topic, value, key):
        self.sent.append((topic, value, key))


async def _envelope(state: AssetState, elements) -> dict:
    p = HqProducer("hq:1", "asset-element-telemetry")
    hq = _FakeKafka()
    p._producer = hq
    await p.publish_snapshot("a1", state, "mrad", elements, False)
    return json.loads(hq.sent[0][1])


@pytest.mark.asyncio
async def test_envelope_carries_condition_and_moved_by_only_where_set() -> None:
    cond = _cond("DEGRADED")
    els = [
        ElementTelemetry("e1", 0, "L", 0.5, 40.0, 10.0),
        ElementTelemetry("e2", 0, "L", 0.95, 40.0, 10.0, moved_by="emission"),
    ]
    env = await _envelope(_state(cond), els)
    assert env["operational"]["condition"] == cond
    assert "moved_by" not in env["elements"][0]
    assert env["elements"][1]["moved_by"] == "emission"
    assert set(env["elements"][0]) == {
        "element_id", "layer_depth", "layer_name", "health", "temp_c",
        "load_pct", "tx_active", "rx_active",
    }


@pytest.mark.asyncio
async def test_envelope_without_condition_has_no_condition_key() -> None:
    env = await _envelope(_state(None), [])
    assert "condition" not in env["operational"]


# 6. config ---------------------------------------------------------

def _load(tmp_path: Path, extra: dict) -> SynthesisKnobs:
    lines = [
        "tick_interval_s: 30",
        "output_topic: asset-element-telemetry",
        "asset_profiles:",
        "  - name: mrad",
        "    matches_platform_variants: [\"MRAD_Sensor\"]",
        "    layers:",
        "      - name: RADAR UNIT",
        "        prefix: TR",
        "    faces:",
        "      - name: PRIMARY APERTURE",
        "        cols: 1",
        "        rows: 1",
        "    synthesis:",
        "      health_nominal_min: 0.55",
    ]
    lines += [f"      {k}: {v}" for k, v in extra.items()]
    f = tmp_path / "c.yaml"
    f.write_text("\n".join(lines) + "\n")
    return SimConfig.load(f).profiles[0].synthesis


def test_condition_knobs_default_and_override(tmp_path: Path) -> None:
    s = _load(tmp_path, {})  # legacy collapsed branch
    assert (s.critical_yellow_fraction, s.critical_red_fraction, s.destroyed_red_fraction) == (0.30, 0.20, 1.0)
    s = _load(tmp_path, {"failed_red_fraction": 0.1})  # per-tier branch
    assert (s.critical_yellow_fraction, s.critical_red_fraction, s.destroyed_red_fraction) == (0.30, 0.20, 1.0)
    s = _load(tmp_path, {"critical_yellow_fraction": 0.4, "critical_red_fraction": 0.3,
                         "destroyed_red_fraction": 0.9})
    assert (s.critical_yellow_fraction, s.critical_red_fraction, s.destroyed_red_fraction) == (0.4, 0.3, 0.9)
