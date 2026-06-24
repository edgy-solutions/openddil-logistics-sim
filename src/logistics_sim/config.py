"""
YAML config loader for openddil-logistics-sim.

Multi-profile: one entry in `asset_profiles[]` per asset TYPE the sim
knows how to populate. Each profile carries its own layer/face layout
(matches the frontend SensorArrayView config for that type), its own
synthesis knobs (a radar runs hotter than a launcher controller), and
its own `matches_platform_variants` filter. MRAD ships as the first
profile; LTAMDS / Patriot / future platforms just add another entry.

A discovered asset routes to the FIRST profile whose
matches_platform_variants list contains its platform_variant. An
asset whose variant matches no profile is ignored -- the sim doesn't
synthesize for things it doesn't have a layout for.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any

import yaml


@dataclasses.dataclass(frozen=True)
class FaceSpec:
    """Depth-0 face spec. cols x rows surface elements per face."""
    name: str
    cols: int
    rows: int


@dataclasses.dataclass(frozen=True)
class LayerSpec:
    """One drill level. Depth 0 ignores cols/rows -- faces[] drives
    its cardinality. Layers 1..N use cols x rows directly."""
    name: str
    prefix: str
    cols: int = 0
    rows: int = 0


@dataclasses.dataclass(frozen=True)
class SynthesisKnobs:
    health_nominal_min: float
    health_nominal_max: float
    # Per-tier fraction of face elements lifted into the yellow (>0.90)
    # and red (>0.97) health bands when the upstream asset reports that
    # tier. NOMINAL implicitly = 0/0. The lift propagates to subtree
    # elements via the rollup cap; a lifted face's children inherit the
    # tier, so an "elevated face fraction" of (yellow+red)=0.25 with an
    # 8-deep tree yields roughly 25% of total elements in yellow/red.
    #
    # User-visible behavior (from the 2026-06-24 demo prep):
    #   NOMINAL  asset (health NOMINAL, power ON) → all green
    #   DEGRADED asset → some yellow only
    #   FAULT    asset → mostly yellow, a few red
    #   FAILED   asset → heavier mix of yellow + red
    #
    # Mapping for non-health-state inputs:
    #   POWER_STATE_OFF / SHUTTING_DOWN  → FAILED
    #   POWER_STATE_MAINTENANCE          → DEGRADED
    #   tx_off AND rx_off (mismatch)     → FAULT
    degraded_yellow_fraction: float
    degraded_red_fraction: float
    fault_yellow_fraction: float
    fault_red_fraction: float
    failed_yellow_fraction: float
    failed_red_fraction: float

    # LEGACY: kept for back-compat with pre-2026-06-24 configs that
    # don't carry the per-tier fields. When NONE of the tier_*_fraction
    # fields are set in the YAML, the loader synthesizes them from
    # degraded_fraction (split 60% yellow / 40% red across all degraded
    # tiers, matching the original collapsed behavior).
    degraded_fraction: float

    temp_min: float
    temp_max: float
    load_min: float
    load_max: float
    tick_drift_temp: float
    tick_drift_load: float


@dataclasses.dataclass(frozen=True)
class AssetProfile:
    """One asset TYPE's layout + synthesis policy. Add a new profile to
    support a new platform (LTAMDS, Patriot, etc.) without touching
    code."""
    name: str
    matches_platform_variants: tuple[str, ...]
    layers: tuple[LayerSpec, ...]
    faces: tuple[FaceSpec, ...]
    synthesis: SynthesisKnobs


@dataclasses.dataclass(frozen=True)
class SimConfig:
    tick_interval_s: float
    output_topic: str
    profiles: tuple[AssetProfile, ...]

    # PowerState / HealthState enum values (from proto) that count as
    # "asset is degraded" for the per-element synthesis. Defaults err
    # toward "show as degraded if in doubt" -- maintainer demos want
    # the broken-element visual to fire on any non-nominal state.
    degraded_power_states: tuple[str, ...]
    degraded_health_states: tuple[str, ...]

    # Kafka wiring -- env-driven so the same image runs against any
    # cluster.
    edge_clusters: dict[str, str]
    hq_brokers: str
    input_topic: str
    consumer_group_prefix: str

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "SimConfig":
        with open(path, "rt") as f:
            raw: dict[str, Any] = yaml.safe_load(f) or {}

        profiles = tuple(
            _parse_profile(p) for p in raw.get("asset_profiles", [])
        )
        if not profiles:
            raise ValueError(
                f"{path}: at least one entry in asset_profiles[] is required"
            )

        # Kafka wiring -- env wins over YAML.
        edge_clusters = _parse_edge_clusters(
            os.environ.get(
                "LOGISTICS_SIM_EDGE_CLUSTERS",
                "edge-01=openddil-redpanda-edge-01:9092,"
                "edge-02=openddil-redpanda-edge-02:9092,"
                "edge-03=openddil-redpanda-edge-03:9092",
            )
        )
        hq_brokers = os.environ.get(
            "LOGISTICS_SIM_HQ_BROKERS", "openddil-redpanda-hq:19092",
        )
        input_topic = os.environ.get(
            "LOGISTICS_SIM_INPUT_TOPIC", "telemetry-latest-state",
        )
        consumer_group_prefix = os.environ.get(
            "LOGISTICS_SIM_CONSUMER_GROUP_PREFIX", "logistics-sim",
        )

        return cls(
            tick_interval_s=float(raw.get("tick_interval_s", 30)),
            output_topic=str(raw.get("output_topic", "asset-element-telemetry")),
            profiles=profiles,
            degraded_power_states=tuple(
                str(s) for s in raw.get("degraded_power_states", [
                    "POWER_STATE_OFF",
                    "POWER_STATE_SHUTTING_DOWN",
                    "POWER_STATE_MAINTENANCE",
                ])
            ),
            degraded_health_states=tuple(
                str(s) for s in raw.get("degraded_health_states", [
                    "HEALTH_STATE_DEGRADED",
                    "HEALTH_STATE_FAULT",
                    "HEALTH_STATE_FAILED",
                ])
            ),
            edge_clusters=edge_clusters,
            hq_brokers=hq_brokers,
            input_topic=input_topic,
            consumer_group_prefix=consumer_group_prefix,
        )

    def profile_for_variant(self, platform_variant: str) -> AssetProfile | None:
        """First profile whose matches_platform_variants includes this
        variant. None when the variant isn't covered by any profile --
        the discovery loop drops those assets."""
        for p in self.profiles:
            if platform_variant in p.matches_platform_variants:
                return p
        return None

    @property
    def all_matched_variants(self) -> frozenset[str]:
        """Union of every profile's matches list. Discovery uses this
        as the cheap pre-filter before consulting profile_for_variant."""
        out: set[str] = set()
        for p in self.profiles:
            out.update(p.matches_platform_variants)
        return frozenset(out)


def _parse_profile(raw: dict[str, Any]) -> AssetProfile:
    layers = tuple(
        LayerSpec(
            name=str(l["name"]),
            prefix=str(l["prefix"]),
            cols=int(l.get("cols", 0)),
            rows=int(l.get("rows", 0)),
        )
        for l in raw.get("layers", [])
    )
    faces = tuple(
        FaceSpec(
            name=str(f["name"]),
            cols=int(f["cols"]),
            rows=int(f["rows"]),
        )
        for f in raw.get("faces", [])
    )
    s = raw.get("synthesis", {})
    legacy_fraction = float(s.get("degraded_fraction", 0.15))

    # Per-tier fraction fields. If ALL six per-tier fields are absent
    # from the YAML, fall back to the legacy degraded_fraction split
    # 60% yellow / 40% red across all degraded tiers -- preserves the
    # pre-2026-06-24 collapsed behavior for old configs.
    tier_keys = (
        "degraded_yellow_fraction", "degraded_red_fraction",
        "fault_yellow_fraction", "fault_red_fraction",
        "failed_yellow_fraction", "failed_red_fraction",
    )
    any_per_tier = any(k in s for k in tier_keys)
    if any_per_tier:
        # Pull each field with a sensible default. Default scale per
        # the 2026-06-24 demo prep (DEGRADED yellow only, FAULT mostly
        # yellow + some red, FAILED heavier mix).
        degraded_yellow = float(s.get("degraded_yellow_fraction", 0.15))
        degraded_red    = float(s.get("degraded_red_fraction",    0.00))
        fault_yellow    = float(s.get("fault_yellow_fraction",    0.20))
        fault_red       = float(s.get("fault_red_fraction",       0.05))
        failed_yellow   = float(s.get("failed_yellow_fraction",   0.30))
        failed_red      = float(s.get("failed_red_fraction",      0.20))
    else:
        # Legacy back-compat: collapse degraded_fraction across tiers.
        degraded_yellow = legacy_fraction * 0.6
        degraded_red    = legacy_fraction * 0.4
        fault_yellow    = legacy_fraction * 0.6
        fault_red       = legacy_fraction * 0.4
        failed_yellow   = legacy_fraction * 0.6
        failed_red      = legacy_fraction * 0.4

    synthesis = SynthesisKnobs(
        health_nominal_min=float(s.get("health_nominal_min", 0.55)),
        health_nominal_max=float(s.get("health_nominal_max", 0.85)),
        degraded_yellow_fraction=degraded_yellow,
        degraded_red_fraction=degraded_red,
        fault_yellow_fraction=fault_yellow,
        fault_red_fraction=fault_red,
        failed_yellow_fraction=failed_yellow,
        failed_red_fraction=failed_red,
        degraded_fraction=legacy_fraction,
        temp_min=float(s.get("temp_min", 30)),
        temp_max=float(s.get("temp_max", 75)),
        load_min=float(s.get("load_min", 5)),
        load_max=float(s.get("load_max", 95)),
        tick_drift_temp=float(s.get("tick_drift_temp", 1.5)),
        tick_drift_load=float(s.get("tick_drift_load", 5.0)),
    )
    return AssetProfile(
        name=str(raw["name"]),
        matches_platform_variants=tuple(
            str(v) for v in raw.get("matches_platform_variants", [])
        ),
        layers=layers,
        faces=faces,
        synthesis=synthesis,
    )


def _parse_edge_clusters(spec: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "=" not in entry:
            raise ValueError(
                f"LOGISTICS_SIM_EDGE_CLUSTERS entry {entry!r} must be 'edge_id=host:port'"
            )
        edge_id, brokers = entry.split("=", 1)
        out[edge_id.strip()] = brokers.strip()
    return out
