"""
Per-element telemetry generator.

Deterministic per (asset_id, element_id, tick_bucket) -- same asset
stays consistent across re-renders, different assets are independent,
tick advancement produces movement.

Honors upstream `AssetState` (snapshot of operational_state +
actively_tx/rx from the customer's telemetry-latest-state feed):

  * When the asset is in a degraded power_state / health_state,
    `degraded` is set True by the tick loop and synthesis.degraded_
    fraction of elements jump to the >0.90 (DEGRADED) or >0.97
    (CRITICAL) band.

  * Face elements (depth 0, T/R modules) honor actively_transmitting
    and actively_receiving FROM THE CUSTOMER SIM directly -- those
    fields ride along on every published element so the maintainer
    view's interrogation panel can show "TX off" / "RX off" badges
    that match the customer's reported state. Non-face elements
    (support electronics) default tx_active=rx_active=True.

  * When tx OR rx is reported off but power_state is supposed to be
    ON, that's a state mismatch -- treated as degraded (face elements
    that "can't transmit when they should" lean toward higher health
    values = more red).
"""
from __future__ import annotations

import dataclasses
import hashlib
import random
from typing import Iterator

from .config import FaceSpec, LayerSpec, SynthesisKnobs


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
            # Asset claims to be ON but is transmitting nothing AND
            # receiving nothing -- maintenance-relevant for the
            # operator regardless of how the customer feed labeled
            # the health_state.
            return True
        return False


@dataclasses.dataclass(frozen=True)
class ElementTelemetry:
    """One element's instantaneous values.

    tx_active / rx_active are propagated FROM THE CUSTOMER SIM via the
    asset's OperationalState.actively_transmitting /
    actively_receiving fields. Face elements (depth 0, T/R modules)
    inherit the asset-level flags directly so the maintainer view can
    show their tx/rx state in lockstep with what the customer feed
    reports. Internal layers (BACKPLANE / PROCESSOR / CHIP) default
    to both true -- they're support electronics, not radio modules.
    """
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


def _element_ids_for_layer(
    depth: int,
    layer: LayerSpec,
    faces: tuple[FaceSpec, ...],
) -> Iterator[str]:
    """Same id format the frontend SensorArrayView generates -- locked
    in by test_element_id_format_matches_frontend."""
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
    """Build a full per-asset element-telemetry snapshot for one tick.

    `degraded` is computed from asset_state.is_degraded(...) -- no
    longer a free knob from the caller. The synthesis bumps face
    elements into the DEGRADED / CRITICAL band when the upstream
    customer-sim state is non-nominal, so what the maintainer sees on
    the 3D drill-down is in sync with the rolled-up state the
    operator already saw at the asset level.
    """
    out: list[ElementTelemetry] = []
    degraded = asset_state.is_degraded(degraded_power_states, degraded_health_states)
    asset_tx = asset_state.actively_transmitting
    asset_rx = asset_state.actively_receiving

    for depth, layer in enumerate(layers):
        for elem_id in _element_ids_for_layer(depth, layer, faces):
            rng = _seeded_rng(asset_id, elem_id, tick_bucket)

            # Health -- nominal band unless asset-level degraded fires.
            health = rng.uniform(synthesis.health_nominal_min, synthesis.health_nominal_max)
            if degraded and rng.random() < synthesis.degraded_fraction:
                if rng.random() < 0.4:
                    health = rng.uniform(0.97, 1.00)
                else:
                    health = rng.uniform(0.90, 0.97)

            # Temp ramps with health; tick drift adds Gaussian wander.
            base_temp = synthesis.temp_min + health * (synthesis.temp_max - synthesis.temp_min)
            temp_c = base_temp + rng.gauss(0, synthesis.tick_drift_temp)

            base_load = rng.uniform(synthesis.load_min, synthesis.load_max)
            load_pct = max(0.0, min(100.0, base_load + rng.gauss(0, synthesis.tick_drift_load)))

            # tx/rx per element:
            #   Face elements (depth 0) ARE the T/R modules -- they
            #   inherit asset-level tx/rx directly.
            #   Internal elements are support electronics -- always
            #   both true (they're not radios).
            if depth == 0:
                tx_active = asset_tx
                rx_active = asset_rx
            else:
                tx_active = True
                rx_active = True

            out.append(ElementTelemetry(
                element_id=elem_id,
                layer_depth=depth,
                layer_name=layer.name,
                health=round(max(0.0, min(1.0, health)), 4),
                temp_c=round(temp_c, 2),
                load_pct=round(load_pct, 1),
                tx_active=tx_active,
                rx_active=rx_active,
            ))
    return out


def cardinality(layers: tuple[LayerSpec, ...], faces: tuple[FaceSpec, ...]) -> int:
    """Count of elements one snapshot will contain."""
    total = 0
    for depth, layer in enumerate(layers):
        if depth == 0:
            total += sum(f.cols * f.rows for f in faces)
        else:
            total += layer.cols * layer.rows
    return total
