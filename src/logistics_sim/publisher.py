"""
HQ publisher for per-asset element-telemetry snapshots.

Payload envelope (one Kafka record per asset per tick, JSON):

  {
    "asset_id": "<asset_id>",
    "platform_variant": "<variant>",
    "profile_name": "<asset_profiles[].name>",
    "observed_at_ns": <int>,
    "operational": {
      "power_state": "POWER_STATE_ON",
      "health_state": "HEALTH_STATE_NOMINAL",
      "actively_transmitting": true,
      "actively_receiving": true,
      "degraded": false
    },
    "elements": [
      {"element_id": ..., "layer_depth": ..., "layer_name": ...,
       "health": ..., "temp_c": ..., "load_pct": ...,
       "tx_active": ..., "rx_active": ...},
      ...
    ]
  }

The operational block surfaces what the customer sim said about the
asset alongside the per-element data, so a downstream consumer
(maintainer UI banner, future RTI exporter) can show "asset is
maintaining" or "tx is off" without re-deriving from per-element bits.

2026-06-30: HqProducer also publishes per-asset PER-LAYER inventory
aggregates to a second topic (asset-element-inventory). One message
per (asset_id, layer_name) per tick. The projector handler upserts
inventory_items rows keyed by `<asset_id>:<layer_name>`. Drives the
maintainer view's "Local FOB Inventory" card so its bars track the
SAME signal the 3D drill-down's tile colors track -- when elements
flip to DEGRADED/FAULT/FAILED, allocated climbs and the bar drops.

Inventory envelope (one Kafka record per (asset, layer) per tick):

  {
    "asset_id": "<asset_id>",
    "layer_name": "T/R MODULE",
    "platform_variant": "<variant>",
    "available_count": 91,
    "allocated_count": 5,
    "total_count":     96,
    "observed_at_ns": <int>
  }
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time

from aiokafka import AIOKafkaProducer

from .element_gen import AssetState, ElementTelemetry

log = logging.getLogger("logistics_sim.publisher")


# Health threshold above which an element is considered DEGRADED/FAULT/
# FAILED for inventory accounting. Mirrors element_gen's `_tier` band
# boundary (> 0.90 = "spare consumed"). Keeping the same number in both
# places means the maintainer view's tile colors and inventory bars
# move from the same signal -- when one fires, the other follows.
_INVENTORY_DEGRADED_HEALTH_THRESHOLD = 0.90


class HqProducer:
    """Wraps AIOKafkaProducer with the per-asset envelope encoder.

    Publishes to TWO topics:
      * `topic` (the asset-element-telemetry topic, per-element tree)
      * `inventory_topic` (asset-element-inventory, per-layer aggregate)
    Both share one underlying AIOKafkaProducer.
    """

    def __init__(
        self, brokers: str, topic: str, inventory_topic: str = "asset-element-inventory",
    ) -> None:
        self._brokers = brokers
        self._topic = topic
        self._inventory_topic = inventory_topic
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._brokers,
            acks=1,
            enable_idempotence=False,
            # Per-asset element-tree envelope can run ~5MB (96 face × 6
            # boards × 4 modules × 9 chips = 23,712 elements at ~170B
            # each). Default 1MB max_request_size would reject it.
            # Topic-level max.message.bytes must match (see docker-
            # compose redpanda-init).
            max_request_size=16 * 1024 * 1024,
        )
        await self._producer.start()
        log.info(
            "HQ producer started (brokers=%s element_topic=%s inventory_topic=%s)",
            self._brokers, self._topic, self._inventory_topic,
        )

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def publish_snapshot(
        self,
        asset_id: str,
        asset_state: AssetState,
        profile_name: str,
        elements: list[ElementTelemetry],
        degraded: bool,
        core_temp_c: float | None = None,
        uptime_hours: float | None = None,
    ) -> None:
        if self._producer is None:
            raise RuntimeError("HqProducer.publish_snapshot called before start()")
        operational: dict = {
            "power_state": asset_state.power_state,
            "health_state": asset_state.health_state,
            "actively_transmitting": asset_state.actively_transmitting,
            "actively_receiving": asset_state.actively_receiving,
            "degraded": degraded,
        }
        # Asset-level rollup metrics the customer-overlay feed doesn't
        # populate (no sustainment block). logistics-sim fills the gap
        # — see element_gen.compute_asset_metrics. Lands in the row's
        # operational JSONB; the maintainer view's CORE TEMP / UPTIME
        # readouts pick them up via useAssetElementTelemetry.
        if core_temp_c is not None:
            operational["core_temp_c"] = core_temp_c
        if uptime_hours is not None:
            operational["uptime_hours"] = uptime_hours
        envelope = {
            "asset_id": asset_id,
            "platform_variant": asset_state.platform_variant,
            "profile_name": profile_name,
            "observed_at_ns": time.time_ns(),
            "operational": operational,
            "elements": [dataclasses.asdict(e) for e in elements],
        }
        payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        await self._producer.send_and_wait(
            self._topic, value=payload, key=asset_id.encode("utf-8"),
        )

    async def publish_inventory(
        self,
        asset_id: str,
        asset_state: AssetState,
        elements: list[ElementTelemetry],
    ) -> int:
        """Per-layer inventory aggregate. One Kafka message per
        (asset, layer); the projector handler upserts each as one
        inventory_items row keyed by `<asset_id>:<layer_name>`.

        Returns the number of messages published (one per layer
        present in `elements`), so the caller can log/metric the
        tick volume.

        The aggregation rule: an element counts as "allocated"
        (spare consumed) when its health is ABOVE the threshold
        used by the maintainer view's tile-color band (>0.90 =
        DEGRADED/FAULT/FAILED). All other elements are "available"
        (still-good spares). Same threshold the tile colors use
        (see element_gen._tier) so the two visualizations move
        from the same signal -- when tiles flip rose, the bar
        drops by the same count.
        """
        if self._producer is None:
            raise RuntimeError("HqProducer.publish_inventory called before start()")

        # Group elements by layer_name, counting allocated / total.
        per_layer: dict[str, dict[str, int]] = {}
        for e in elements:
            slot = per_layer.setdefault(
                e.layer_name, {"total": 0, "allocated": 0},
            )
            slot["total"] += 1
            if e.health > _INVENTORY_DEGRADED_HEALTH_THRESHOLD:
                slot["allocated"] += 1

        observed_at_ns = time.time_ns()
        published = 0
        for layer_name, counts in per_layer.items():
            allocated = counts["allocated"]
            total = counts["total"]
            envelope = {
                "asset_id": asset_id,
                "layer_name": layer_name,
                "platform_variant": asset_state.platform_variant,
                "available_count": total - allocated,
                "allocated_count": allocated,
                "total_count": total,
                "observed_at_ns": observed_at_ns,
            }
            payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
            # Key by `asset_id:layer_name` so a compacted-topic deploy
            # would naturally retain only the latest row per layer.
            # Today's topic isn't compacted (asset-element-inventory is
            # add-only at the broker, the projector handles deduplication
            # via upsert on id) but keying right is free insurance.
            key = f"{asset_id}:{layer_name}".encode("utf-8")
            await self._producer.send_and_wait(
                self._inventory_topic, value=payload, key=key,
            )
            published += 1
        return published
