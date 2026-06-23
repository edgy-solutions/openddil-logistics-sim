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
"""
from __future__ import annotations

import dataclasses
import json
import logging
import time

from aiokafka import AIOKafkaProducer

from .element_gen import AssetState, ElementTelemetry

log = logging.getLogger("logistics_sim.publisher")


class HqProducer:
    """Wraps AIOKafkaProducer with the per-asset envelope encoder."""

    def __init__(self, brokers: str, topic: str) -> None:
        self._brokers = brokers
        self._topic = topic
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
        log.info("HQ producer started (brokers=%s topic=%s)",
                 self._brokers, self._topic)

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
    ) -> None:
        if self._producer is None:
            raise RuntimeError("HqProducer.publish_snapshot called before start()")
        envelope = {
            "asset_id": asset_id,
            "platform_variant": asset_state.platform_variant,
            "profile_name": profile_name,
            "observed_at_ns": time.time_ns(),
            "operational": {
                "power_state": asset_state.power_state,
                "health_state": asset_state.health_state,
                "actively_transmitting": asset_state.actively_transmitting,
                "actively_receiving": asset_state.actively_receiving,
                "degraded": degraded,
            },
            "elements": [dataclasses.asdict(e) for e in elements],
        }
        payload = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        await self._producer.send_and_wait(
            self._topic, value=payload, key=asset_id.encode("utf-8"),
        )
