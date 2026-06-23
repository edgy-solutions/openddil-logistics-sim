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
    # Mirrors config/default.yaml's mrad profile and the frontend
    # MRAD_CONFIG.layers exactly. Tree is 96 × 4 × 4 × 4 = 8,160
    # elements per asset (reduced from the original 23,712 to keep
    # the per-asset wire payload under ~1.6MB so integrated GPUs on
    # work machines can hold WebGL context on first load).
    return (
        LayerSpec(name="RADAR UNIT",     prefix="TR",     cols=0, rows=0),
        LayerSpec(name="BACKPLANE",      prefix="BOARD",  cols=2, rows=2),
        LayerSpec(name="PROCESSOR BANK", prefix="MODULE", cols=2, rows=2),
        LayerSpec(name="GAN MMIC CHIP",  prefix="CHIP",   cols=2, rows=2),
    )


@pytest.fixture
def mrad_faces() -> tuple[FaceSpec, ...]:
    # 6×8 = 48 face elements (was 8×12 = 96). Shrunk to keep WebGL
    # context alive on work-cluster integrated GPU. Must match
    # config/default.yaml + frontend MRAD_CONFIG.faces.
    return (FaceSpec(name="PRIMARY APERTURE", cols=6, rows=8),)


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
    # Tree topology — depth-0 face × (per-layer cols×rows)^depth fanout.
    # 48 + 48*4 + 48*4*4 + 48*4*4*4 = 4,080.
    expected = 48 + 48 * 4 + 48 * 4 * 4 + 48 * 4 * 4 * 4
    assert cardinality(mrad_layers, mrad_faces) == expected
    assert expected == 4_080


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
    """Locks in the frontend contract — path-encoded element ids. The
    frontend's SensorArrayView constructs the same ids using its
    drillPath stack + per-cell (i,j); both sides MUST agree byte-for-
    byte or the lookup misses."""
    snap = generate_snapshot(
        asset_id="any-asset",
        asset_state=_nominal_state(),
        layers=mrad_layers, faces=mrad_faces, synthesis=synthesis,
        tick_bucket=0,
        degraded_power_states=DEGRADED_POWER,
        degraded_health_states=DEGRADED_HEALTH,
    )
    surface = re.compile(r"^TR-PRIMARYAPERTURE-\d+-\d+$")
    backplane = re.compile(r"^TR-PRIMARYAPERTURE-\d+-\d+/BOARD-\d+-\d+$")
    proc = re.compile(r"^TR-PRIMARYAPERTURE-\d+-\d+/BOARD-\d+-\d+/MODULE-\d+-\d+$")
    chip = re.compile(
        r"^TR-PRIMARYAPERTURE-\d+-\d+/BOARD-\d+-\d+/MODULE-\d+-\d+/CHIP-\d+-\d+$"
    )
    for e in snap:
        if e.layer_depth == 0:
            assert surface.match(e.element_id), e.element_id
        elif e.layer_depth == 1:
            assert backplane.match(e.element_id), e.element_id
        elif e.layer_depth == 2:
            assert proc.match(e.element_id), e.element_id
        elif e.layer_depth == 3:
            assert chip.match(e.element_id), e.element_id


def test_rollup_invariant_no_child_exceeds_parent_severity(mrad_layers, mrad_faces, synthesis):
    """Drill-down consistency: a child's severity tier never exceeds
    its parent's. Drilling into a NOMINAL board MUST NOT show a
    CRITICAL module under it."""
    state = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_FAULT",  # asset is degraded → some severity to roll up
    )
    snap = generate_snapshot("asset-rollup", state,
                             mrad_layers, mrad_faces, synthesis, tick_bucket=3,
                             degraded_power_states=DEGRADED_POWER,
                             degraded_health_states=DEGRADED_HEALTH)
    by_id = {e.element_id: e for e in snap}
    for e in snap:
        if "/" not in e.element_id:
            continue  # depth 0 has no parent
        parent_id = e.element_id.rsplit("/", 1)[0]
        parent = by_id[parent_id]
        if parent.health <= 0.90:
            assert e.health <= 0.90 + 1e-6, (
                f"NOMINAL parent {parent_id} ({parent.health}) "
                f"has non-NOMINAL child {e.element_id} ({e.health})"
            )
        elif parent.health <= 0.97:
            assert e.health <= 0.97 + 1e-6, (
                f"DEGRADED parent {parent_id} ({parent.health}) "
                f"has CRITICAL child {e.element_id} ({e.health})"
            )
        # CRITICAL parent — no constraint on child


def test_rollup_invariant_at_least_one_child_matches_parent_tier(mrad_layers, mrad_faces, synthesis):
    """Drilling into a yellow or red parent MUST surface at least one
    child in the same tier — otherwise the maintenance drill-down
    fails the "follow the red" UX."""
    state = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_FAULT",
    )
    snap = generate_snapshot("asset-match", state,
                             mrad_layers, mrad_faces, synthesis, tick_bucket=7,
                             degraded_power_states=DEGRADED_POWER,
                             degraded_health_states=DEGRADED_HEALTH)
    children_by_parent: dict[str, list] = {}
    for e in snap:
        if "/" not in e.element_id:
            continue
        parent_id = e.element_id.rsplit("/", 1)[0]
        children_by_parent.setdefault(parent_id, []).append(e)
    by_id = {e.element_id: e for e in snap}
    checked = 0
    for parent_id, children in children_by_parent.items():
        parent = by_id[parent_id]
        if parent.health <= 0.90:
            continue  # NOMINAL parent — no obligation
        checked += 1
        if parent.health > 0.97:
            assert any(c.health > 0.97 for c in children), (
                f"CRITICAL parent {parent_id} ({parent.health}) "
                f"has no CRITICAL child (children: {[c.health for c in children]})"
            )
        else:  # DEGRADED parent
            assert any(c.health > 0.90 for c in children), (
                f"DEGRADED parent {parent_id} ({parent.health}) "
                f"has no DEGRADED-or-CRITICAL child (children: {[c.health for c in children]})"
            )
    assert checked > 0, "test must actually exercise non-NOMINAL parents"


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


def test_tick_advancement_keeps_health_stable_drifts_temp_load(mrad_layers, mrad_faces, synthesis):
    """Demo stability invariant: health (which drives color tier) is
    seeded WITHOUT tick_bucket, so it stays constant across every
    tick of the run. temp_c and load_pct keep a small per-tick gauss
    drift so numeric readouts still move (otherwise the UI looks
    frozen). Operators walking the maintainer view see stable colors;
    a yellow tile stays yellow across multiple sim ticks. The earlier
    contract (health varies per tick) was demo-hostile — switching
    happens on asset_state.is_degraded transitions instead, not on
    the wall clock."""
    t0 = generate_snapshot("asset-A", _nominal_state(),
                           mrad_layers, mrad_faces, synthesis, tick_bucket=0,
                           degraded_power_states=DEGRADED_POWER,
                           degraded_health_states=DEGRADED_HEALTH)
    t1 = generate_snapshot("asset-A", _nominal_state(),
                           mrad_layers, mrad_faces, synthesis, tick_bucket=1,
                           degraded_power_states=DEGRADED_POWER,
                           degraded_health_states=DEGRADED_HEALTH)
    # Same element id at two ticks → same health.
    h0 = {e.element_id: e.health for e in t0}
    h1 = {e.element_id: e.health for e in t1}
    assert h0 == h1, "health values must be tick-invariant for demo stability"
    # temp/load DO drift (so the UI numeric panels still move per tick).
    temps0 = {e.element_id: e.temp_c for e in t0}
    temps1 = {e.element_id: e.temp_c for e in t1}
    assert temps0 != temps1, "temp_c should still drift per tick (small gauss noise)"


def test_asset_degraded_transition_changes_health(mrad_layers, mrad_faces, synthesis):
    """When the asset's operational state flips from nominal to
    degraded (or back), the deterministic ~15% subset elevates (or
    returns to nominal). This is the ONLY thing that moves color
    tiers — not the wall clock. Without this, the maintainer view
    couldn't react to a customer-feed-reported fault."""
    nominal = _nominal_state()
    degraded = AssetState(
        platform_variant="MRAD2_radar",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_FAULT",
    )
    snap_nominal = generate_snapshot("asset-X", nominal,
                                     mrad_layers, mrad_faces, synthesis, tick_bucket=42,
                                     degraded_power_states=DEGRADED_POWER,
                                     degraded_health_states=DEGRADED_HEALTH)
    snap_degraded = generate_snapshot("asset-X", degraded,
                                      mrad_layers, mrad_faces, synthesis, tick_bucket=42,
                                      degraded_power_states=DEGRADED_POWER,
                                      degraded_health_states=DEGRADED_HEALTH)
    # Many elements MUST differ — the degraded synthesis branch fired
    # for ~15% of them.
    h_nom = {e.element_id: e.health for e in snap_nominal}
    h_deg = {e.element_id: e.health for e in snap_degraded}
    diffs = sum(1 for k in h_nom if h_nom[k] != h_deg[k])
    assert diffs > 0, "degraded transition must elevate some elements"


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
