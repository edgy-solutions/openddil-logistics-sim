"""
Element generation correctness + invariants.

Locks in:
  * Cardinality matches the configured layout.
  * Element ids match the frontend SensorArrayView format byte-for-byte.
  * Per-(asset, tick) determinism + per-asset independence.
  * Values stay in bounds.
  * Tick advancement causes movement.
  * Degraded-band fraction reaches the >0.90 critical/degraded band.
  * Upstream operational_state -> degraded plumbing fires correctly.
  * Upstream actively_tx / actively_rx propagate per-element on face
    elements and are unconditionally True on internal layers.
"""
from __future__ import annotations

import re

import pytest

from logistics_sim.config import FaceSpec, LayerSpec, SynthesisKnobs
from logistics_sim.element_gen import AssetState, cardinality, generate_snapshot


@pytest.fixture
def mrad_layers() -> tuple[LayerSpec, ...]:
    return (
        LayerSpec(name="RADAR UNIT",     prefix="TR",     cols=0, rows=0),
        LayerSpec(name="BACKPLANE",      prefix="BOARD",  cols=2, rows=3),
        LayerSpec(name="PROCESSOR BANK", prefix="MODULE", cols=2, rows=2),
        LayerSpec(name="GAN MMIC CHIP",  prefix="CHIP",   cols=3, rows=3),
    )


@pytest.fixture
def mrad_faces() -> tuple[FaceSpec, ...]:
    return (FaceSpec(name="PRIMARY APERTURE", cols=8, rows=12),)


@pytest.fixture
def synthesis() -> SynthesisKnobs:
    return SynthesisKnobs(
        health_nominal_min=0.55, health_nominal_max=0.85,
        degraded_fraction=0.15,
        temp_min=30, temp_max=75,
        load_min=5, load_max=95,
        tick_drift_temp=1.5, tick_drift_load=5.0,
    )


DEGRADED_POWER = ("POWER_STATE_OFF", "POWER_STATE_SHUTTING_DOWN", "POWER_STATE_MAINTENANCE")
DEGRADED_HEALTH = ("HEALTH_STATE_DEGRADED", "HEALTH_STATE_FAULT", "HEALTH_STATE_FAILED")


def _nominal_state(variant: str = "MRAD2_radar") -> AssetState:
    return AssetState(
        platform_variant=variant,
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=True,
        actively_receiving=True,
    )


def test_cardinality_matches_layout(mrad_layers, mrad_faces):
    assert cardinality(mrad_layers, mrad_faces) == 96 + 6 + 4 + 9


def test_snapshot_size_matches_cardinality(mrad_layers, mrad_faces, synthesis):
    snap = generate_snapshot(
        asset_id="demo:mrad-sensor-001",
        asset_state=_nominal_state(),
        layers=mrad_layers, faces=mrad_faces, synthesis=synthesis,
        tick_bucket=42,
        degraded_power_states=DEGRADED_POWER,
        degraded_health_states=DEGRADED_HEALTH,
    )
    assert len(snap) == cardinality(mrad_layers, mrad_faces)


def test_element_id_format_matches_frontend(mrad_layers, mrad_faces, synthesis):
    """Locks in the frontend contract -- element id format MUST match
    SensorArrayView.generateElements() byte-for-byte."""
    snap = generate_snapshot(
        asset_id="any-asset",
        asset_state=_nominal_state(),
        layers=mrad_layers, faces=mrad_faces, synthesis=synthesis,
        tick_bucket=0,
        degraded_power_states=DEGRADED_POWER,
        degraded_health_states=DEGRADED_HEALTH,
    )
    surface = re.compile(r"^TR-PRIMARYAPERTURE-\d+-\d+$")
    backplane = re.compile(r"^BOARD-\d+-\d+$")
    proc = re.compile(r"^MODULE-\d+-\d+$")
    chip = re.compile(r"^CHIP-\d+-\d+$")
    for e in snap:
        if e.layer_depth == 0:
            assert surface.match(e.element_id), e.element_id
        elif e.layer_depth == 1:
            assert backplane.match(e.element_id), e.element_id
        elif e.layer_depth == 2:
            assert proc.match(e.element_id), e.element_id
        elif e.layer_depth == 3:
            assert chip.match(e.element_id), e.element_id


def test_deterministic_per_asset_and_tick(mrad_layers, mrad_faces, synthesis):
    a = generate_snapshot("asset-A", _nominal_state(),
                          mrad_layers, mrad_faces, synthesis, tick_bucket=100,
                          degraded_power_states=DEGRADED_POWER,
                          degraded_health_states=DEGRADED_HEALTH)
    b = generate_snapshot("asset-A", _nominal_state(),
                          mrad_layers, mrad_faces, synthesis, tick_bucket=100,
                          degraded_power_states=DEGRADED_POWER,
                          degraded_health_states=DEGRADED_HEALTH)
    assert {(e.element_id, e.health, e.temp_c, e.load_pct) for e in a} == \
           {(e.element_id, e.health, e.temp_c, e.load_pct) for e in b}


def test_different_assets_independent(mrad_layers, mrad_faces, synthesis):
    a = generate_snapshot("asset-A", _nominal_state(),
                          mrad_layers, mrad_faces, synthesis, tick_bucket=0,
                          degraded_power_states=DEGRADED_POWER,
                          degraded_health_states=DEGRADED_HEALTH)
    b = generate_snapshot("asset-B", _nominal_state(),
                          mrad_layers, mrad_faces, synthesis, tick_bucket=0,
                          degraded_power_states=DEGRADED_POWER,
                          degraded_health_states=DEGRADED_HEALTH)
    assert [e.health for e in a] != [e.health for e in b]


def test_values_in_bounds(mrad_layers, mrad_faces, synthesis):
    snap = generate_snapshot("asset-X", _nominal_state(),
                             mrad_layers, mrad_faces, synthesis, tick_bucket=7,
                             degraded_power_states=DEGRADED_POWER,
                             degraded_health_states=DEGRADED_HEALTH)
    for e in snap:
        assert 0.0 <= e.health <= 1.0
        assert e.temp_c >= synthesis.temp_min - 5 * synthesis.tick_drift_temp
        assert e.temp_c <= synthesis.temp_max + 5 * synthesis.tick_drift_temp
        assert 0.0 <= e.load_pct <= 100.0


def test_tick_advancement_moves_values(mrad_layers, mrad_faces, synthesis):
    t0 = generate_snapshot("asset-A", _nominal_state(),
                           mrad_layers, mrad_faces, synthesis, tick_bucket=0,
                           degraded_power_states=DEGRADED_POWER,
                           degraded_health_states=DEGRADED_HEALTH)
    t1 = generate_snapshot("asset-A", _nominal_state(),
                           mrad_layers, mrad_faces, synthesis, tick_bucket=1,
                           degraded_power_states=DEGRADED_POWER,
                           degraded_health_states=DEGRADED_HEALTH)
    assert [e.health for e in t0] != [e.health for e in t1]


# -----------------------------------------------------------------------------
# operational_state plumbing
# -----------------------------------------------------------------------------
def test_degraded_power_state_fires(mrad_layers, mrad_faces, synthesis):
    state = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_MAINTENANCE",
        health_state="HEALTH_STATE_NOMINAL",
    )
    assert state.is_degraded(DEGRADED_POWER, DEGRADED_HEALTH)


def test_degraded_health_state_fires(mrad_layers, mrad_faces, synthesis):
    state = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_FAULT",
    )
    assert state.is_degraded(DEGRADED_POWER, DEGRADED_HEALTH)


def test_nominal_state_not_degraded():
    assert not _nominal_state().is_degraded(DEGRADED_POWER, DEGRADED_HEALTH)


def test_degraded_state_pushes_elements_to_critical_band(mrad_layers, mrad_faces, synthesis):
    """When upstream reports degraded, the synthesis must lift SOME
    elements into the >0.90 band. This is what makes the maintainer
    3D view light up red when the customer feed flags an asset."""
    state = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_FAULT",
    )
    high = 0
    total = 0
    for tb in range(20):
        snap = generate_snapshot(
            "asset-Z", state,
            mrad_layers, mrad_faces, synthesis, tick_bucket=tb,
            degraded_power_states=DEGRADED_POWER,
            degraded_health_states=DEGRADED_HEALTH,
        )
        for e in snap:
            total += 1
            if e.health > 0.90:
                high += 1
    assert high > 0, "degraded state must push SOME elements over 0.90"
    assert high / total < 0.5, "fraction should be a fraction, not majority"


# -----------------------------------------------------------------------------
# tx/rx synchronization with customer-sim state
# -----------------------------------------------------------------------------
def test_face_elements_inherit_asset_tx(mrad_layers, mrad_faces, synthesis):
    """Face elements ARE the T/R modules -- they must report the same
    actively_transmitting bit the upstream customer sim reports."""
    tx_off = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=False,
        actively_receiving=True,
    )
    snap = generate_snapshot("asset-T", tx_off,
                             mrad_layers, mrad_faces, synthesis, tick_bucket=0,
                             degraded_power_states=DEGRADED_POWER,
                             degraded_health_states=DEGRADED_HEALTH)
    face = [e for e in snap if e.layer_depth == 0]
    internal = [e for e in snap if e.layer_depth > 0]
    assert face and all(not e.tx_active for e in face), \
        "every face T/R module must report tx_active=False when asset.tx=False"
    assert all(e.rx_active for e in face), \
        "rx_active must remain True when only tx is off"
    assert all(e.tx_active and e.rx_active for e in internal), \
        "internal support electronics always tx_active=rx_active=True"


def test_face_elements_inherit_asset_rx(mrad_layers, mrad_faces, synthesis):
    """Mirror of the tx test for rx -- the actively_receiving bit must
    flow through to every face element."""
    rx_off = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=True,
        actively_receiving=False,
    )
    snap = generate_snapshot("asset-R", rx_off,
                             mrad_layers, mrad_faces, synthesis, tick_bucket=0,
                             degraded_power_states=DEGRADED_POWER,
                             degraded_health_states=DEGRADED_HEALTH)
    face = [e for e in snap if e.layer_depth == 0]
    assert face and all(not e.rx_active for e in face), \
        "every face T/R module must report rx_active=False when asset.rx=False"
    assert all(e.tx_active for e in face), \
        "tx_active must remain True when only rx is off"


def test_both_tx_and_rx_off_is_degraded():
    """The 'asset claims to be ON but is neither tx-ing nor rx-ing'
    state mismatch -- treated as degraded for synthesis purposes
    even when power_state and health_state are nominal."""
    s = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=False,
        actively_receiving=False,
    )
    assert s.is_degraded(DEGRADED_POWER, DEGRADED_HEALTH)


def test_tx_off_alone_does_not_degrade_synthesis():
    """tx OR rx off (but not both) is a SIGNAL, not a degraded state
    -- the per-element tx/rx fields reflect it but the asset's
    health/temp/load synthesis stays nominal."""
    s_tx = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=False,
        actively_receiving=True,
    )
    s_rx = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=True,
        actively_receiving=False,
    )
    assert not s_tx.is_degraded(DEGRADED_POWER, DEGRADED_HEALTH)
    assert not s_rx.is_degraded(DEGRADED_POWER, DEGRADED_HEALTH)


def test_default_tx_rx_true_when_unspecified():
    """A customer feed that doesn't carry OperationalState shouldn't
    produce every face element with tx/rx=off -- defaults are True."""
    s = AssetState(platform_variant="MRAD2_radar")
    assert s.actively_transmitting is True
    assert s.actively_receiving is True
