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
    methods complete atomically -- no lock needed."""

    def __init__(self) -> None:
        self._assets: dict[str, AssetState] = {}

    def upsert(self, asset_id: str, state: AssetState) -> bool:
        """Returns True iff this is a NEW asset (first sight). Used by
        discovery to log discoveries without spamming on every tick."""
        is_new = asset_id not in self._assets
        self._assets[asset_id] = state
        return is_new

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


def _asset_state_from_json(data: dict) -> tuple[str, str, AssetState] | None:
    """JSON-encoded EntityTelemetryEvent shape. Pulls (asset_id,
    platform_variant, AssetState) -- returns None if asset_id or
    platform_variant is missing."""
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
    )
    return str(asset_id), str(variant), state


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
      * If `HasField` raises (proto3 scalar)   -> use the wire value;
        proto3 cannot distinguish 'unset' from 'explicit false', so
        defaulting on absence is impossible here.
    """
    try:
        if not msg.HasField(field_name):
            return default
    except ValueError:
        pass  # proto3 scalar without optional -- value-only path
    return bool(getattr(msg, field_name))


def _asset_state_from_proto(raw: bytes) -> tuple[str, str, AssetState] | None:
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
    )
    return ev.asset.asset_id, ev.asset.platform_variant, state


def _extract(raw: bytes) -> tuple[str, str, AssetState] | None:
    """Try JSON first (covers overlay proprietary feeds), then proto
    (covers the DIS + faust-edge produced events). Both produce the
    same triple."""
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
    """
    variants = frozenset(matched_variants)
    canonical_map = variant_canonical_map or {}
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
        "aliases=%d)",
        edge_id, brokers, input_topic, consumer_group, len(canonical_map),
    )
    try:
        async for msg in consumer:
            extracted = _extract(msg.value)
            if not extracted:
                continue
            asset_id, native_variant, state = extracted
            # Canonicalize via the alias map. Unknown -> passthrough,
            # which matches the dev/docker-compose case where the wire
            # is already canonical (alias file may not be mounted).
            canonical = canonical_map.get(native_variant, native_variant)
            if canonical not in variants:
                continue
            # Downstream (tick loop -> profile_for_variant) matches
            # against canonical too, so rewrite the AssetState's
            # platform_variant to the canonical token. AssetState is
            # frozen; use dataclasses.replace.
            if canonical != native_variant:
                state = dataclasses.replace(state, platform_variant=canonical)
            is_new = roster.upsert(asset_id, state)
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
