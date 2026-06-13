"""
logistics-sim entrypoint.

Wires:
  * Config load (YAML at LOGISTICS_SIM_CONFIG_PATH).
  * One discovery consumer per edge cluster (asyncio task per edge).
  * One HQ producer.
  * One tick loop that, every tick_interval_s, walks the current roster,
    routes each asset to its asset_profile by platform_variant, builds
    a synthesized snapshot, and publishes.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys

from .asset_discovery import AssetRoster, run_edge_discovery
from .config import SimConfig
from .element_gen import cardinality, generate_snapshot
from .publisher import HqProducer

log = logging.getLogger("logistics_sim.main")


async def _tick_loop(
    cfg: SimConfig,
    roster: AssetRoster,
    producer: HqProducer,
) -> None:
    # Pre-compute per-profile cardinality for the startup log.
    per_profile_card = {p.name: cardinality(p.layers, p.faces) for p in cfg.profiles}
    log.info("tick loop starting (interval=%.1fs, profiles=%s, cardinality=%s)",
             cfg.tick_interval_s,
             [p.name for p in cfg.profiles],
             per_profile_card)

    while True:
        try:
            tick_bucket = int(_clock() // cfg.tick_interval_s)
            snapshot = roster.snapshot()
            if not snapshot:
                log.debug("tick %d: roster empty, skipping", tick_bucket)
            published = 0
            degraded_count = 0
            for asset_id, asset_state in snapshot.items():
                profile = cfg.profile_for_variant(asset_state.platform_variant)
                if profile is None:
                    # Should be unreachable -- discovery only adds assets
                    # whose variant is in all_matched_variants. Defensive
                    # log + skip in case config is reloaded narrower mid-
                    # run.
                    log.warning("no profile for variant %s (asset %s); skipping",
                                asset_state.platform_variant, asset_id)
                    continue
                elements = generate_snapshot(
                    asset_id=asset_id,
                    asset_state=asset_state,
                    layers=profile.layers,
                    faces=profile.faces,
                    synthesis=profile.synthesis,
                    tick_bucket=tick_bucket,
                    degraded_power_states=cfg.degraded_power_states,
                    degraded_health_states=cfg.degraded_health_states,
                )
                degraded = asset_state.is_degraded(
                    cfg.degraded_power_states, cfg.degraded_health_states,
                )
                if degraded:
                    degraded_count += 1
                await producer.publish_snapshot(
                    asset_id=asset_id,
                    asset_state=asset_state,
                    profile_name=profile.name,
                    elements=elements,
                    degraded=degraded,
                )
                published += 1
            if published:
                log.info(
                    "tick %d: published %d snapshot(s) (%d degraded honoring upstream state)",
                    tick_bucket, published, degraded_count,
                )
        except asyncio.CancelledError:
            log.info("tick loop cancelled")
            raise
        except Exception:
            log.exception("tick loop iteration failed; will retry next interval")
        await asyncio.sleep(cfg.tick_interval_s)


def _clock() -> float:
    import time
    return time.time()


async def _serve(cfg: SimConfig) -> int:
    roster = AssetRoster()
    producer = HqProducer(cfg.hq_brokers, cfg.output_topic)
    await producer.start()

    matched = cfg.all_matched_variants
    log.info("discovering assets with platform_variant in %s", sorted(matched))

    tasks: list[asyncio.Task[None]] = []
    for edge_id, brokers in cfg.edge_clusters.items():
        consumer_group = f"{cfg.consumer_group_prefix}-{edge_id}"
        tasks.append(asyncio.create_task(
            run_edge_discovery(
                edge_id=edge_id,
                brokers=brokers,
                input_topic=cfg.input_topic,
                consumer_group=consumer_group,
                matched_variants=matched,
                roster=roster,
            ),
            name=f"discovery-{edge_id}",
        ))
    tasks.append(asyncio.create_task(
        _tick_loop(cfg, roster, producer), name="tick-loop",
    ))

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _request_stop() -> None:
        log.info("stop signal received; draining tasks")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            pass

    await stop_event.wait()
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("task %s raised during shutdown", t.get_name())
    await producer.stop()
    return 0


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stdout,
    )
    config_path = os.environ.get(
        "LOGISTICS_SIM_CONFIG_PATH",
        "/etc/openddil/logistics-sim/config.yaml",
    )
    if not os.path.exists(config_path):
        config_path = "/app/config/default.yaml"
    log.info("loading config from %s", config_path)
    cfg = SimConfig.load(config_path)
    log.info(
        "logistics-sim starting -- profiles=%s tick=%ss output=%s edges=%s",
        [p.name for p in cfg.profiles], cfg.tick_interval_s,
        cfg.output_topic, list(cfg.edge_clusters.keys()),
    )
    try:
        return asyncio.run(_serve(cfg))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
