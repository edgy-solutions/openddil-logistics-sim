"""
Asset discovery + state tracking from telemetry-latest-state.

Two jobs in one consumer:

  1. Discover assets whose platform_variant matches one of the
     configured asset_profiles (MRAD first; LTAMDS / Patriot to follow
     as config-only additions).

  2. Track per-asset OperationalState snapshots -- power_state,
     health_state, actively_transmitting, actively_receiving -- so
     the tick loop can honor the customer-sim-reported state in
     every element snapshot. Discovery is ALWAYS the freshest source
     of these flags: every new telemetry-latest-state record
     overwrites the cached AssetState atomically (single asyncio
     event loop, no lock needed).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import Iterable, Mapping

from aiokafka import AIOKafkaConsumer

from .element_gen import AssetState

log = logging.getLogger("logistics_sim.discovery")


# PowerState / HealthState enum value names. Mirrors the proto enum,
# we materialize both name AND int form (since proto-deserialized
# instances expose int, JSON-shaped instances expose the string name).
_POWER_STATE_NAME = {
    0: "POWER_STATE_UNSPECIFIED",
    1: "POWER_STATE_OFF",
    2: "POWER_STATE_ON",
    3: "POWER_STATE_STANDBY",
    4: "POWER_STATE_MAINTENANCE",
    5: "POWER_STATE_SHUTTING_DOWN",
}
_HEALTH_STATE_NAME = {
    0: "HEALTH_STATE_UNSPECIFIED",
    1: "HEALTH_STATE_NOMINAL",
    2: "HEALTH_STATE_DEGRADED",
    3: "HEALTH_STATE_FAULT",
    4: "HEALTH_STATE_FAILED",
}


class AssetRoster:
    """Asset_id -> AssetState. Thread-safety: single asyncio loop,
    methods complete atomically -- no lock needed.

    2026-06-30: holds an asyncio.Event ("changed") that fires when an
    upsert lands a TIER-RELEVANT field change (power_state, health_state,
    actively_transmitting, actively_receiving) or a new asset. The
    tick loop races sleep(tick_interval_s) against this event so a
    customer-side state flip surfaces on the maintainer view within
    ~1s instead of waiting up to one full tick cycle (~30s, or worse
    when per-asset publish cost pushes the effective cycle beyond
    tick_interval_s). Bulk-change bursts coalesce naturally -- an
    Event set N times is the same as set once.

    The event is created lazily on first access so unit tests that
    construct AssetRoster outside an asyncio context still work.
    """

    # Fields that affect element_gen.severity_tier / generate_snapshot
    # output. A change in any of these means the next tick should
    # produce a visibly-different per-element tree, so it's worth
    # waking the tick loop early. Other AssetState fields (e.g.
    # platform_variant) don't influence the per-element synthesis,
    # so we don't fire on those.
    _TIER_RELEVANT_FIELDS: tuple[str, ...] = (
        "power_state",
        "health_state",
        "actively_transmitting",
        "actively_receiving",
    )

    def __init__(self) -> None:
        self._assets: dict[str, AssetState] = {}
        # asset_id -> edge that last reported it. Last report wins; an
        # upsert without an edge id leaves the known owner in place.
        self._edges: dict[str, str] = {}
        self._changed_event: asyncio.Event | None = None

    @property
    def changed_event(self) -> asyncio.Event:
        """Lazily-created event. Tests that only call .upsert()/.snapshot()
        won't hit asyncio.Event() construction (which is loop-bound on
        older Pythons and would fail outside a running loop)."""
        if self._changed_event is None:
            self._changed_event = asyncio.Event()
        return self._changed_event

    def _is_tier_change(self, old: AssetState, new: AssetState) -> bool:
        for f in self._TIER_RELEVANT_FIELDS:
            if getattr(old, f, None) != getattr(new, f, None):
                return True
        # Compare the condition's level and moved_by only. Its claims carry
        # observed_at, which changes on every record; waking the loop on
        # that would republish every few seconds for nothing.
        oc, nc = old.condition or {}, new.condition or {}
        return (oc.get("level") != nc.get("level")
                or oc.get("moved_by") != nc.get("moved_by"))

    def upsert(
        self, asset_id: str, state: AssetState, edge_id: str | None = None,
    ) -> bool:
        """Returns True iff this is a NEW asset (first sight). Used by
        discovery to log discoveries without spamming on every tick.

        Also: when this upsert lands a tier-relevant change (or this
        is a new asset), signal `changed_event` so the tick loop can
        wake early. The event is created lazily on first access; in
        the rare case it doesn't exist yet (no tick loop ever called
        .changed_event), upserts are silent -- correct since there's
        no waiter to signal."""
        prev = self._assets.get(asset_id)
        is_new = prev is None
        self._assets[asset_id] = state
        if edge_id is not None:
            self._edges[asset_id] = edge_id
        if self._changed_event is not None:
            if is_new or self._is_tier_change(prev, state):
                self._changed_event.set()
        return is_new

    def edge_of(self, asset_id: str) -> str | None:
        """Edge that most recently reported this asset, or None."""
        return self._edges.get(asset_id)

    def snapshot(self) -> dict[str, AssetState]:
        """Frozen view for the tick loop. Returns a shallow copy so a
        concurrent upsert from the discovery task doesn't mutate the
        dict the tick loop is iterating."""
        return dict(self._assets)

    def __len__(self) -> int:
        return len(self._assets)


def _coerce_power_state(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return _POWER_STATE_NAME.get(value, "POWER_STATE_UNSPECIFIED")
    return "POWER_STATE_UNSPECIFIED"


def _coerce_health_state(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, int):
        return _HEALTH_STATE_NAME.get(value, "HEALTH_STATE_UNSPECIFIED")
    return "HEALTH_STATE_UNSPECIFIED"


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lo = value.lower()
        if lo in ("true", "1", "yes"):
            return True
        if lo in ("false", "0", "no"):
            return False
    return default


def _kills_from(data: dict) -> frozenset[str]:
    """Subsystem kills from Silver `active_fault_codes`.

    Reads the "SUBSYS:HEALTH" form the DIS appearance mapping emits and
    fusion's _eval_subsystems already parses — one vocabulary, no translation
    layer. Only INOPERATIVE counts as a kill: DEGRADED is impairment, and
    conflating the two would let a worn subsystem cap an asset the same way a
    destroyed one does.

    An EMPTY set means the tactical plane said nothing. It must never be read
    as "no kills" — that is the absence-as-nominal mistake, and the caller
    (tactical_cap) is written so silence yields no cap at all.
    """
    codes = (((data.get("sustainment") or {}).get("health") or {})
             .get("active_fault_codes") or [])
    out = set()
    for code in codes:
        if not isinstance(code, str) or ":" not in code:
            continue
        subsys, health = code.split(":", 1)
        if health.strip().upper() == "INOPERATIVE":
            out.add(subsys.strip().upper())
    return frozenset(out)


def _declared_subsystem(value) -> str | None:
    """Normalize a declared AssetIdentity.subsystem to its enum name.
    Absent, empty, or ASSET_SUBSYSTEM_UNSPECIFIED -> None."""
    if not value or value in (0, "ASSET_SUBSYSTEM_UNSPECIFIED"):
        return None
    return str(value)


def subsystem_matches(required: str, declared: str | None) -> bool:
    """A profile's match_subsystem gate. Empty `required` disables the
    filter; otherwise the event must declare exactly that subsystem."""
    return not required or declared == required


def _condition_from_json(value: object) -> dict | None:
    """The Condition as proto JSON, kept only when it is a dict with a
    string level; anything else reads as no claim."""
    if isinstance(value, dict) and isinstance(value.get("level"), str):
        return value
    return None


def _condition_from_proto(op) -> dict | None:
    """The Condition as proto JSON (field names, enum names, RFC3339
    observed_at). Bindings without the field read as no claim."""
    try:
        if not op.HasField("condition"):
            return None
    except ValueError:
        return None
    from google.protobuf import json_format
    return json_format.MessageToDict(op.condition, preserving_proto_field_name=True)


def _asset_state_from_json(
    data: dict,
) -> tuple[str, str, AssetState, str | None] | None:
    """JSON-encoded EntityTelemetryEvent shape. Pulls (asset_id,
    platform_variant, AssetState) -- returns None if asset_id or
    platform_variant is missing. The declared asset subsystem rides
    along as a fourth element (None when absent or unspecified)."""
    asset = data.get("asset") or {}
    asset_id = asset.get("asset_id") or data.get("asset_id")
    variant = asset.get("platform_variant") or data.get("platform_variant")
    if not asset_id or not variant:
        return None
    op = data.get("operational_state") or {}
    state = AssetState(
        platform_variant=str(variant),
        power_state=_coerce_power_state(op.get("power_state")),
        health_state=_coerce_health_state(op.get("health_state")),
        actively_transmitting=_coerce_bool(op.get("actively_transmitting"), True),
        actively_receiving=_coerce_bool(op.get("actively_receiving"), True),
        subsystem_kills=_kills_from(data),
        condition=_condition_from_json(op.get("condition")),
    )
    return (
        str(asset_id), str(variant), state,
        _declared_subsystem(asset.get("subsystem")),
    )


def _proto_bool_with_default(msg, field_name: str, default: bool) -> bool:
    """Read a proto bool field with sane default-on-absence semantics
    across BOTH proto2/proto3-optional AND plain proto3 scalars.

    Plain proto3 scalars (no `optional` keyword) DO NOT support
    `HasField` -- calling it raises `ValueError: Field X does not have
    presence`. That killed the sim's discovery consumer on the very
    first message it tried to decode (edge-01 only, because edges
    without inbound data sit idle and never call this path).

    Behavior:
      * If `HasField` works AND field is unset -> return `default`.
      * If `HasField` works AND field is set   -> return the value.
      * If `HasField` raises (proto3 scalar)   -> a wire True is a
        positive claim and is returned; a wire False cannot be told
        from unset, so it returns `default`. An absent claim must not
        become a fault (ADR-0026 amendment: absence is not health).
        The explicit-false claim given up here returns once the
        fields carry presence.
    """
    try:
        if not msg.HasField(field_name):
            return default
    except ValueError:
        # proto3 scalar without optional -- value-only path
        return True if getattr(msg, field_name) else default
    return bool(getattr(msg, field_name))


def _asset_state_from_proto(
    raw: bytes,
) -> tuple[str, str, AssetState, str | None] | None:
    """Proto-encoded EntityTelemetryEvent path. Same triple, populated
    via the proto field accessors so enum ints become enum names via
    the lookup tables above."""
    try:
        from openddil.telemetry.v1 import telemetry_pb2  # type: ignore
        ev = telemetry_pb2.EntityTelemetryEvent()
        ev.ParseFromString(raw)
    except Exception:
        log.debug("proto decode failed; skipping")
        return None
    if not ev.asset.asset_id or not ev.asset.platform_variant:
        return None
    op = ev.operational_state
    state = AssetState(
        platform_variant=ev.asset.platform_variant,
        power_state=_POWER_STATE_NAME.get(op.power_state, "POWER_STATE_UNSPECIFIED"),
        health_state=_HEALTH_STATE_NAME.get(op.health_state, "HEALTH_STATE_UNSPECIFIED"),
        actively_transmitting=_proto_bool_with_default(op, "actively_transmitting", True),
        actively_receiving=_proto_bool_with_default(op, "actively_receiving", True),
        condition=_condition_from_proto(op),
    )
    # Bindings without the field are refused at startup whenever a profile
    # filters on it (_require_subsystem_field), so absence here only means
    # no profile needs it.
    subsystem = None
    field = ev.asset.DESCRIPTOR.fields_by_name.get("subsystem")
    if field is not None:
        value = field.enum_type.values_by_number.get(ev.asset.subsystem)
        subsystem = _declared_subsystem(
            value.name if value is not None else str(ev.asset.subsystem)
        )
    return ev.asset.asset_id, ev.asset.platform_variant, state, subsystem


def _require_subsystem_field() -> None:
    """A profile filters on AssetIdentity.subsystem. Bindings that predate
    the field would read every record as undeclared, and the profile would
    quietly match nothing -- refuse to start instead."""
    try:
        from openddil.telemetry.v1 import telemetry_pb2  # type: ignore
    except ImportError:
        return  # no proto bindings: only the JSON shape is read
    if "subsystem" not in telemetry_pb2.AssetIdentity.DESCRIPTOR.fields_by_name:
        raise RuntimeError(
            "a profile sets match_subsystem but the telemetry bindings have "
            "no AssetIdentity.subsystem; the runtime bundle's contracts are "
            "older than this profile"
        )


def _extract(raw: bytes) -> tuple[str, str, AssetState, str | None] | None:
    """Try JSON first (covers overlay proprietary feeds), then proto
    (covers the DIS + faust-edge produced events). Both produce the
    same tuple: (asset_id, platform_variant, state, declared subsystem)."""
    if not raw:
        return None
    try:
        if raw[:1] in (b"{", b"["):
            data = json.loads(raw)
            return _asset_state_from_json(data)
    except Exception:
        pass
    return _asset_state_from_proto(raw)


async def run_edge_discovery(
    edge_id: str,
    brokers: str,
    input_topic: str,
    consumer_group: str,
    matched_variants: Iterable[str],
    roster: AssetRoster,
    variant_canonical_map: Mapping[str, str] | None = None,
    variant_subsystem_map: Mapping[str, str] | None = None,
) -> None:
    """One coroutine per edge cluster. Updates the per-asset
    AssetState every time a record arrives -- including the tx/rx
    bits that the tick loop uses to drive element-level synthesis.

    `variant_canonical_map` translates upstream proprietary tokens to
    canonical OpenDDIL tokens BEFORE matching against
    `matched_variants`. Required when faust-edge emits
    telemetry-latest-state with un-aliased customer-feed variants
    (the standard cluster setup); harmless when empty (the wire is
    already canonical, every variant looks up to itself).
    Loaded from `platform_variant_aliases.yaml` by main.py.

    `variant_subsystem_map` carries the per-profile declared-subsystem
    filter (AssetProfile.match_subsystem). When the entry for the
    resolved canonical variant is non-empty, the event's declared
    asset.subsystem MUST equal it or the message is skipped. Lets a
    profile target one KIND of asset when multiple kinds share a
    variant (e.g. MRAD: the sensor record declares the sensor
    subsystem and keeps the multi-array profile, the chassis record
    declares none and falls out -- both carry variant=MRAD_Sensor
    after aliasing but only the sensor has the array subsystem the
    profile synthesizes for). asset_id is opaque and never parsed.
    """
    variants = frozenset(matched_variants)
    canonical_map = variant_canonical_map or {}
    subsystem_map = variant_subsystem_map or {}
    if any(subsystem_map.values()):
        _require_subsystem_field()
    consumer = AIOKafkaConsumer(
        input_topic,
        bootstrap_servers=brokers,
        group_id=consumer_group,
        auto_offset_reset="latest",
        enable_auto_commit=True,
    )
    await consumer.start()
    log.info(
        "[%s] discovery consumer started (brokers=%s topic=%s group=%s "
        "aliases=%d subsystem_filters=%d)",
        edge_id, brokers, input_topic, consumer_group,
        len(canonical_map),
        sum(1 for s in subsystem_map.values() if s),
    )
    try:
        async for msg in consumer:
            extracted = _extract(msg.value)
            if not extracted:
                continue
            asset_id, native_variant, state, subsystem = extracted
            # Canonicalize via the alias map. Unknown -> passthrough,
            # which matches the dev/docker-compose case where the wire
            # is already canonical (alias file may not be mounted).
            canonical = canonical_map.get(native_variant, native_variant)
            if canonical not in variants:
                continue
            # Per-profile declared-subsystem filter -- applied AFTER the
            # variant check. Empty string disables the filter for that
            # variant (single-kind-per-variant case).
            if not subsystem_matches(subsystem_map.get(canonical, ""), subsystem):
                continue
            # Downstream (tick loop -> profile_for_variant) matches
            # against canonical too, so rewrite the AssetState's
            # platform_variant to the canonical token. AssetState is
            # frozen; use dataclasses.replace.
            if canonical != native_variant:
                state = dataclasses.replace(state, platform_variant=canonical)
            is_new = roster.upsert(asset_id, state, edge_id)
            if is_new:
                if canonical != native_variant:
                    log.info(
                        "[%s] discovered asset %s variant=%s "
                        "(aliased from native=%s, roster=%d)",
                        edge_id, asset_id, canonical, native_variant,
                        len(roster),
                    )
                else:
                    log.info(
                        "[%s] discovered asset %s variant=%s (roster=%d)",
                        edge_id, asset_id, canonical, len(roster),
                    )
            elif log.isEnabledFor(logging.DEBUG):
                # State refresh -- spam at DEBUG only.
                log.debug(
                    "[%s] state refresh %s power=%s health=%s tx=%s rx=%s",
                    edge_id, asset_id, state.power_state, state.health_state,
                    state.actively_transmitting, state.actively_receiving,
                )
    except asyncio.CancelledError:
        log.info("[%s] discovery consumer cancelled", edge_id)
        raise
    finally:
        await consumer.stop()
