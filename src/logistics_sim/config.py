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

    # Optional asset_id-suffix filter applied AFTER platform_variant
    # matching. Lets a profile match a variant that's used by multiple
    # asset KINDS in the customer wire model but apply only to the
    # right one. Specific case (2026-06-29): after the per-site sensor
    # identity fix in the customer-bundle Bloblang, both per-site
    # SENSORS (asset_id ends in `_Sensor`) and per-site RADAR CHASSIS
    # (asset_id ends in `_radar`) carry platform_variant=MRAD_Sensor
    # (the chassis via alias). Only the sensor has the multi-array
    # subsystem the MRAD profile synthesizes for. With
    # match_asset_id_suffix=`_Sensor`, the chassis falls out of
    # discovery -- no wasted Kafka traffic, no wasted postgres rows,
    # no element telemetry for assets without arrays.
    #
    # Empty string (or absent in YAML) disables the filter -- the
    # profile then matches purely on platform_variant, preserving the
    # pre-2026-06-29 behavior for profiles that don't have the
    # variant-shared-across-kinds problem.
    match_asset_id_suffix: str = ""


@dataclasses.dataclass(frozen=True)
class PartsAvailabilitySite:
    """One `parts_availability.sites` entry. `nearest` is the
    nearest-first distance ring for this site, used later by the
    event assembler (ADR-0046 §4, the picture's spares section) to find
    "nearest site with stock" -- see `spare_picture` below.

    `nation` is None when the site has no nation configured. Unlike
    releasability.py's `site_nation` fallback (which labels an
    undeclared ASSET with the deployment's default nation), there is
    no equivalent default here: a parts-availability site with no
    configured nation stays unlabelled on the wire
    (`originator_nation: null`), deliberately not defaulted."""
    nation: str | None
    nearest: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class PartsAvailabilityPart:
    """One `parts_availability.parts[]` entry. `on_hand` maps site id
    -> static on-hand quantity for this part. Only sites the YAML
    named for this part are keys; a site this part doesn't mention
    has no configured stock (treated as 0 by publishers/helpers).

    `lead_time_days` maps site id -> stand-in days-to-ship figure for
    that site, same "only the sites named" shape as `on_hand` -- but
    here absence is published as absence (no `lead_time_days` key on
    the record), never defaulted to 0, since 0 is itself a real
    figure. `source` names the system a part's figures stand in for
    (`"stand-in"` by default); it labels every record for that part,
    not per-site."""
    part_ref: str
    item: str
    on_hand: dict[str, int]
    lead_time_days: dict[str, int] = dataclasses.field(default_factory=dict)
    source: str = "stand-in"


@dataclasses.dataclass(frozen=True)
class PartsAvailabilityConfig:
    """Spare-parts-availability stand-in config (ADR-0046 §4,
    the picture's spares section). Stock is static configuration, not a simulation
    of consumption -- see publisher.py's publish_parts_availability
    docstring."""
    topic: str
    interval_s: float
    sites: dict[str, PartsAvailabilitySite]
    parts: tuple[PartsAvailabilityPart, ...]


def spare_picture(
    site: str,
    part_ref: str,
    availability: dict[str, dict[str, int]],
    sites: dict[str, PartsAvailabilitySite],
) -> dict[str, Any]:
    """Pure "on hand here / nearest site with stock" lookup (ADR-0046
    §4, the picture's spares section). Called by
    `publisher.HqProducer.publish_parts_availability`, once per
    (site, part) record; the egress assembler reads the resulting
    `nearest_site_with_stock` / `nearest_on_hand` fields off the wire
    record rather than calling this itself.

    `availability` maps part_ref -> {site: on_hand} (e.g.
    `{p.part_ref: p.on_hand for p in cfg.parts}`); `sites` is
    `PartsAvailabilityConfig.sites`.

    Walks `site`'s `nearest` ring in configured (nearest-first) order
    and returns the first OTHER site with on_hand > 0 for `part_ref`.
    With no stock anywhere in the ring, `nearest_site_with_stock` is
    None (not "") and `nearest_on_hand` is 0.
    """
    stock_by_site = availability.get(part_ref, {})
    on_hand_here = stock_by_site.get(site, 0)

    site_spec = sites.get(site)
    nearest_order = site_spec.nearest if site_spec is not None else ()

    for candidate in nearest_order:
        candidate_stock = stock_by_site.get(candidate, 0)
        if candidate_stock > 0:
            return {
                "on_hand_here": on_hand_here,
                "nearest_site_with_stock": candidate,
                "nearest_on_hand": candidate_stock,
            }

    return {
        "on_hand_here": on_hand_here,
        "nearest_site_with_stock": None,
        "nearest_on_hand": 0,
    }


def _parse_parts_availability(
    raw: dict[str, Any], path: str | os.PathLike[str],
) -> PartsAvailabilityConfig:
    pa_raw = raw.get("parts_availability") or {}

    # Env wins over YAML -- same convention as releasability_path /
    # site_nation above. Only the topic and interval are env-
    # overridable; sites/parts are deployment-shape config, not
    # per-process wiring.
    topic = os.environ.get(
        "LOGISTICS_SIM_PARTS_AVAILABILITY_TOPIC",
        str(pa_raw.get("topic", "parts-availability")),
    )
    interval_s = float(os.environ.get(
        "LOGISTICS_SIM_PARTS_AVAILABILITY_INTERVAL_S",
        str(pa_raw.get("interval_s", 60)),
    ))

    sites: dict[str, PartsAvailabilitySite] = {}
    for site_id, site_row in (pa_raw.get("sites") or {}).items():
        site_row = site_row or {}
        sites[str(site_id)] = PartsAvailabilitySite(
            nation=site_row.get("nation"),
            nearest=tuple(str(s) for s in (site_row.get("nearest") or ())),
        )

    # `nearest` entries must reference a known site.
    for site_id, site in sites.items():
        for other in site.nearest:
            if other not in sites:
                raise ValueError(
                    f"{path}: parts_availability.sites.{site_id}.nearest "
                    f"references unknown site {other!r}"
                )

    seen_part_refs: set[str] = set()
    parts: list[PartsAvailabilityPart] = []
    for part_row in pa_raw.get("parts") or []:
        part_ref = str(part_row["part_ref"])
        if part_ref in seen_part_refs:
            raise ValueError(
                f"{path}: parts_availability.parts has duplicate "
                f"part_ref {part_ref!r}"
            )
        seen_part_refs.add(part_ref)

        on_hand: dict[str, int] = {}
        for site_id, qty in (part_row.get("on_hand") or {}).items():
            site_id = str(site_id)
            if site_id not in sites:
                raise ValueError(
                    f"{path}: parts_availability.parts[{part_ref!r}]."
                    f"on_hand references unknown site {site_id!r}"
                )
            qty = int(qty)
            if qty < 0:
                raise ValueError(
                    f"{path}: parts_availability.parts[{part_ref!r}]."
                    f"on_hand[{site_id!r}] is negative ({qty})"
                )
            on_hand[site_id] = qty

        lead_time_days: dict[str, int] = {}
        for site_id, days in (part_row.get("lead_time_days") or {}).items():
            site_id = str(site_id)
            if site_id not in sites:
                raise ValueError(
                    f"{path}: parts_availability.parts[{part_ref!r}]."
                    f"lead_time_days references unknown site {site_id!r}"
                )
            # bool is an int subclass in Python -- reject it explicitly
            # so `true`/`false` in YAML don't silently become 1/0.
            if isinstance(days, bool) or not isinstance(days, int):
                raise ValueError(
                    f"{path}: parts_availability.parts[{part_ref!r}]."
                    f"lead_time_days[{site_id!r}] is not an int ({days!r})"
                )
            if days < 0:
                raise ValueError(
                    f"{path}: parts_availability.parts[{part_ref!r}]."
                    f"lead_time_days[{site_id!r}] is negative ({days})"
                )
            lead_time_days[site_id] = days

        parts.append(PartsAvailabilityPart(
            part_ref=part_ref,
            item=str(part_row.get("item", "")),
            on_hand=on_hand,
            lead_time_days=lead_time_days,
            source=str(part_row.get("source", "stand-in")),
        ))

    return PartsAvailabilityConfig(
        topic=topic,
        interval_s=interval_s,
        sites=sites,
        parts=tuple(parts),
    )


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

    # Deployment identity -- not Kafka wiring, but the same env-wins-
    # over-YAML convention applies. `site_nation` defaults to "" (unset)
    # deliberately: an unset site nation must make an undeclared asset
    # fall through to NO label rather than silently default to some
    # nation, per releasability.py's precedence rules.
    releasability_path: str
    site_nation: str

    # Spare-parts-availability stand-in (ADR-0046 §4, the picture's spares section).
    # Validated at load time -- see _parse_parts_availability. Absent
    # `parts_availability:` block loads to empty sites/parts (nothing
    # published, not an error); a PRESENT block with a bad reference
    # fails start, same "validate on load" posture as the rest of
    # this file.
    parts_availability: PartsAvailabilityConfig

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

        # Deployment identity -- env wins over YAML, same convention as
        # the Kafka wiring above.
        releasability_path = os.environ.get(
            "LOGISTICS_SIM_RELEASABILITY_PATH",
            str(raw.get("releasability_path", "/ontology/releasability.yaml")),
        )
        site_nation = os.environ.get(
            "LOGISTICS_SIM_SITE_NATION",
            str(raw.get("site_nation", "")),
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
            releasability_path=releasability_path,
            site_nation=site_nation,
            parts_availability=_parse_parts_availability(raw, path),
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

    @property
    def variant_suffix_map(self) -> dict[str, str]:
        """Per-variant asset_id suffix filter (from AssetProfile.
        match_asset_id_suffix). Empty string means "no filter -- match
        any asset_id with that variant."

        The set of keys is identical to all_matched_variants; discovery
        uses this to apply the suffix filter on each message after the
        cheap variant pre-check passes. Multiple profiles can share a
        variant in principle; in that case the last profile in the
        config wins (degenerate but harmless -- maintain that the
        config author avoids the collision)."""
        out: dict[str, str] = {}
        for p in self.profiles:
            for v in p.matches_platform_variants:
                out[v] = p.match_asset_id_suffix
        return out


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
        match_asset_id_suffix=str(raw.get("match_asset_id_suffix", "")),
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
