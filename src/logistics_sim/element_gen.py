"""
Per-element telemetry generator — TREE TOPOLOGY with parent-child rollup.

Each MRAD asset is modelled as a true tree:

  TR (96 face elements)
    └── BOARD (6 per TR — 576 total)
          └── MODULE (4 per board — 2,304 total)
                └── CHIP (9 per module — 20,736 total)
  Total: 23,712 elements per asset.

Element ids are PATH-ENCODED so each parent's children are distinct from
every other parent's children:

  TR-PRIMARYAPERTURE-3-5/BOARD-0-1/MODULE-1-1/CHIP-0-2

Drilling into TR-A and TR-B at the maintainer view shows DIFFERENT BOARD-0-0
elements (because their paths differ), and each board's children are
independent in turn. This is what gives the demo intuitive consistency
when a maintainer drills into a yellow square and expects to find the
yellow component underneath.

Severity ROLLS UP top-down via the max-rollup invariant:

  * A child's severity tier never exceeds its parent's. NOMINAL parent →
    all children NOMINAL (health ≤ 0.89). DEGRADED parent → children
    NOMINAL or DEGRADED (health ≤ 0.965). CRITICAL parent → no cap.
  * At least ONE child shares the parent's tier (when parent is DEGRADED
    or CRITICAL). Without this, drilling into a yellow board could
    surface all-green modules, which defeats the maintenance UX.

Determinism: per (asset_id, element_id, tick_bucket). Same asset + same
tick → identical tree. Different tick → values move (noise drift only;
the structure is identical).

Honors upstream `AssetState`:
  * When the asset reports degraded power_state / health_state (or the
    tx-and-rx-both-off mismatch), `degraded` fires and ~degraded_fraction
    of FACE elements get bumped into the >0.90 band. Their subtrees
    inherit via the rollup invariant above.
  * Face elements (depth 0, T/R modules) inherit asset-level
    actively_transmitting / actively_receiving directly. Internal
    elements (BACKPLANE / PROCESSOR / CHIP) are support electronics —
    always tx_active=rx_active=True.
"""
from __future__ import annotations

import dataclasses
import hashlib
import random
from typing import Iterator

from .config import FaceSpec, LayerSpec, SynthesisKnobs


# Severity thresholds — MUST match the frontend's getStatusFromHealth.
_TIER_CRITICAL = 0.97
_TIER_DEGRADED = 0.90


@dataclasses.dataclass(frozen=True)
class AssetState:
    """Snapshot of the customer-sim-reported state per asset. Discovery
    populates this from each telemetry-latest-state record; the tick
    loop uses it to drive synthesis."""
    platform_variant: str
    power_state: str = "POWER_STATE_UNSPECIFIED"
    health_state: str = "HEALTH_STATE_UNSPECIFIED"
    # Default to True so a customer feed that doesn't populate the
    # OperationalState block doesn't show every face element with
    # tx/rx off (that would be the wrong demo signal).
    actively_transmitting: bool = True
    actively_receiving: bool = True

    def is_degraded(self, degraded_power_states: tuple[str, ...],
                    degraded_health_states: tuple[str, ...]) -> bool:
        """The synthesis "this asset is broken" predicate. Power-OFF /
        SHUTTING_DOWN / MAINTENANCE OR health DEGRADED / FAULT /
        FAILED, OR the state mismatch case (asset is supposed to be
        ON but reports tx and rx both off)."""
        if self.power_state in degraded_power_states:
            return True
        if self.health_state in degraded_health_states:
            return True
        if not self.actively_transmitting and not self.actively_receiving:
            return True
        return False


@dataclasses.dataclass(frozen=True)
class ElementTelemetry:
    """One element's instantaneous values."""
    element_id: str
    layer_depth: int
    layer_name: str
    health: float
    temp_c: float
    load_pct: float
    tx_active: bool = True
    rx_active: bool = True


def _seeded_rng(asset_id: str, element_id: str, tick_bucket: int) -> random.Random:
    key = f"{asset_id}|{element_id}|{tick_bucket}".encode("utf-8")
    digest = hashlib.sha1(key).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False)
    return random.Random(seed)


def _tier(health: float) -> str:
    if health > _TIER_CRITICAL:
        return "CRITICAL"
    if health > _TIER_DEGRADED:
        return "DEGRADED"
    return "NOMINAL"


def _cap_to_parent_tier(health: float, parent_health: float | None) -> float:
    """Max-rollup invariant: a child's health (severity) never exceeds
    its parent's. NOMINAL parent caps children below DEGRADED threshold;
    DEGRADED parent caps below CRITICAL; CRITICAL parent imposes no cap.
    parent_health=None (depth 0, no parent) imposes no cap either."""
    if parent_health is None:
        return health
    if parent_health > _TIER_CRITICAL:
        return health
    if parent_health > _TIER_DEGRADED:
        return min(health, _TIER_CRITICAL - 0.005)
    return min(health, _TIER_DEGRADED - 0.01)


def _ensure_at_least_one_match(
    child_healths: list[float],
    parent_health: float | None,
    invariant_rng: random.Random,
) -> list[float]:
    """If parent is DEGRADED or CRITICAL but no child has reached that
    tier (after capping), promote the first child so the maintainer
    drill-down always shows at least one matching child. Without this,
    drilling into a yellow board could surface all-green modules — the
    original demo bug this whole refactor exists to fix."""
    if parent_health is None:
        return child_healths
    parent_tier = _tier(parent_health)
    if parent_tier == "NOMINAL":
        return child_healths
    if any(_tier(h) == parent_tier for h in child_healths):
        return child_healths
    if parent_tier == "CRITICAL":
        child_healths[0] = invariant_rng.uniform(_TIER_CRITICAL + 0.005, 1.00)
    else:  # DEGRADED
        child_healths[0] = invariant_rng.uniform(_TIER_DEGRADED + 0.005, _TIER_CRITICAL - 0.005)
    return child_healths


def generate_snapshot(
    asset_id: str,
    asset_state: AssetState,
    layers: tuple[LayerSpec, ...],
    faces: tuple[FaceSpec, ...],
    synthesis: SynthesisKnobs,
    tick_bucket: int,
    degraded_power_states: tuple[str, ...],
    degraded_health_states: tuple[str, ...],
) -> list[ElementTelemetry]:
    """Build a full per-asset element-telemetry tree for one tick.

    Walks the configured layer hierarchy depth-first, emitting one
    ElementTelemetry per node. Path-encoded element ids make each
    parent's children distinct from every other parent's. Top-down
    rollup ensures children never exceed parent severity.

    DEMO STABILITY: health values are seeded WITHOUT the tick_bucket
    (i.e. `_seeded_rng(asset_id, elem_id, 0)`), so a given element's
    health — and therefore its color tier — is constant across every
    tick of the run. Operators walking the maintainer view see a
    stable picture; a degraded TR stays degraded, a nominal one stays
    nominal. temp_c and load_pct keep a small per-tick gauss drift so
    the numeric readouts move a little (looks "alive" without flopping
    the tier). When asset_state.is_degraded flips (customer feed
    reports a fault), the SAME deterministic subset of elements
    elevates — visible color change, but still stable across ticks
    until the asset-level state flips back.
    """
    out: list[ElementTelemetry] = []
    degraded_asset = asset_state.is_degraded(degraded_power_states, degraded_health_states)
    asset_tx = asset_state.actively_transmitting
    asset_rx = asset_state.actively_receiving

    def _synth_health(elem_id: str, parent_health: float | None) -> float:
        # Tick-invariant seed: same element always gets the same health
        # roll, so tier colors don't flop between ticks. The degraded
        # bump uses the same RNG so the same ~15% of elements light up
        # whenever the asset is in a degraded state.
        rng = _seeded_rng(asset_id, elem_id, 0)
        health = rng.uniform(synthesis.health_nominal_min, synthesis.health_nominal_max)
        if degraded_asset and rng.random() < synthesis.degraded_fraction:
            if rng.random() < 0.4:
                health = rng.uniform(_TIER_CRITICAL, 1.00)
            else:
                health = rng.uniform(_TIER_DEGRADED, _TIER_CRITICAL)
        return _cap_to_parent_tier(health, parent_health)

    def _build_node(
        elem_id: str, depth: int, layer_name: str, health: float,
    ) -> None:
        # Two RNG streams: stable (per element, never varies) anchors
        # the baseline temp/load values; drift (per element per tick)
        # adds the small per-tick gauss wander.
        stable = _seeded_rng(asset_id, elem_id, 0)
        drift = _seeded_rng(asset_id, elem_id, tick_bucket)
        # Burn one draw on `stable` to match _synth_health's RNG state
        # cost — keeps the load draw uncorrelated from the health draw.
        stable.random()
        base_temp = synthesis.temp_min + health * (synthesis.temp_max - synthesis.temp_min)
        temp_c = base_temp + drift.gauss(0, synthesis.tick_drift_temp)
        base_load = stable.uniform(synthesis.load_min, synthesis.load_max)
        load_pct = max(0.0, min(100.0, base_load + drift.gauss(0, synthesis.tick_drift_load)))
        # Face elements ARE the T/R modules — inherit asset-level tx/rx.
        # Internal layers are support electronics — always both true.
        tx_active = asset_tx if depth == 0 else True
        rx_active = asset_rx if depth == 0 else True
        out.append(ElementTelemetry(
            element_id=elem_id,
            layer_depth=depth,
            layer_name=layer_name,
            health=round(max(0.0, min(1.0, health)), 4),
            temp_c=round(temp_c, 2),
            load_pct=round(load_pct, 1),
            tx_active=tx_active,
            rx_active=rx_active,
        ))

    def _recurse(parent_id: str | None, parent_health: float | None, depth: int) -> None:
        if depth >= len(layers):
            return
        layer = layers[depth]

        if depth == 0:
            # Face elements — one grid per FaceSpec. No parent constraint.
            for face in faces:
                face_token = face.name.replace(" ", "")
                cell_specs: list[tuple[str, float]] = []
                for i in range(face.cols):
                    for j in range(face.rows):
                        elem_id = f"{layer.prefix}-{face_token}-{i}-{j}"
                        h = _synth_health(elem_id, None)
                        cell_specs.append((elem_id, h))
                for elem_id, h in cell_specs:
                    _build_node(elem_id, 0, layer.name, h)
                    _recurse(elem_id, h, 1)
            return

        # Internal layer — generate children of parent_id.
        assert parent_id is not None
        cell_specs2: list[tuple[str, float]] = []
        for i in range(layer.cols):
            for j in range(layer.rows):
                elem_id = f"{parent_id}/{layer.prefix}-{i}-{j}"
                h = _synth_health(elem_id, parent_health)
                cell_specs2.append((elem_id, h))

        # Enforce "at least one child shares parent tier" using a
        # TICK-INVARIANT seed (third arg = 0). Demo stability: same
        # parent always promotes the same child, so colors don't
        # rearrange tick-to-tick.
        invariant_rng = _seeded_rng(asset_id, parent_id + "|invariant", 0)
        healths = _ensure_at_least_one_match(
            [h for _, h in cell_specs2], parent_health, invariant_rng,
        )

        for (elem_id, _orig_h), h in zip(cell_specs2, healths):
            _build_node(elem_id, depth, layer.name, h)
            _recurse(elem_id, h, depth + 1)

    _recurse(None, None, 0)
    return out


def compute_asset_metrics(asset_id: str, tick_bucket: int) -> tuple[float, float]:
    """Asset-level rollup metrics the customer-overlay feed doesn't carry
    (no sustainment block, no hour meters). logistics-sim fills the gap
    so the maintainer view's CORE TEMP and UPTIME readouts move with the
    asset instead of showing the 32.0 / 1,422H fallback literals.

    core_temp_c: stable per asset (a "ground truth" baseline seeded by
    asset_id, range 28-45°C). Each asset shows a distinct value; same
    asset shows the same value across ticks. Looks like a real per-
    chassis baseline that the operator might compare against.

    uptime_hours: monotonically increasing. baseline seeded per asset
    in the 500-3000 hour range (so an asset reads as "running for a
    while" rather than "just powered on") plus tick_bucket * (30/3600)
    so the value advances 1 hour per ~120 ticks (=1 real hour at the
    default 30s tick interval). Stable within a tick; advances slowly
    enough that the operator perceives it as "system uptime ticking
    upward" without flopping.
    """
    rng = _seeded_rng(asset_id, "__asset_metrics__", 0)
    core_temp_c = round(28.0 + rng.random() * 17.0, 1)
    # Stable per asset, no per-tick increment. tick_bucket here is
    # epoch-anchored (time.time() // tick_interval_s), so multiplying
    # it by anything yields nonsense like 495,000h ≈ 56 years. We'd
    # need asset-first-seen tracking in AssetRoster to compute real
    # "since-discovery" advancement; for the demo we just lock the
    # baseline so the readout looks like an aged hour-meter that
    # doesn't flop tick-to-tick. tick_bucket is accepted so future
    # callers can wire real advancement without an API change.
    _ = tick_bucket
    uptime_hours = round(500.0 + rng.random() * 2500.0, 1)
    return core_temp_c, uptime_hours


def cardinality(layers: tuple[LayerSpec, ...], faces: tuple[FaceSpec, ...]) -> int:
    """Total elements in the materialized tree. Depth 0 cardinality is
    the sum of face grids; each successive depth multiplies by the
    layer's cols×rows fanout."""
    if not layers:
        return 0
    depth_0 = sum(f.cols * f.rows for f in faces)
    total = depth_0
    fanout = depth_0
    for d in range(1, len(layers)):
        layer = layers[d]
        per_parent = layer.cols * layer.rows
        fanout *= per_parent
        total += fanout
    return total


# Kept for backward compat with any external caller that imported it.
def _element_ids_for_layer(
    depth: int,
    layer: LayerSpec,
    faces: tuple[FaceSpec, ...],
) -> Iterator[str]:  # pragma: no cover - shape preserved, no longer used internally
    """DEPRECATED: flat-id helper. The tree generator uses path-encoded
    ids inside generate_snapshot now. This function is retained only as
    a contract-shim — do not introduce new callers."""
    if depth == 0:
        for face in faces:
            face_token = face.name.replace(" ", "")
            for i in range(face.cols):
                for j in range(face.rows):
                    yield f"{layer.prefix}-{face_token}-{i}-{j}"
    else:
        for i in range(layer.cols):
            for j in range(layer.rows):
                yield f"{layer.prefix}-{i}-{j}"
