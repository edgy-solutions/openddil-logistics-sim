"""
Arrival-declaration feed for logistics-sim's releasability labels.

A person's arrival declaration (ADR-0050) outranks every other label
source, so this module keeps the sim's view of them current. The
`asset-declarations` topic is compacted and keyed by `asset_id`; the value
is JSON carrying `originator_nation` and `releasable_to`. A null value (a
tombstone) retracts the declaration. We read it from the HQ brokers, where
every edge's declarations bridge up, so one consumer sees them all.

The consumer reads from earliest on start with no group (the map is rebuilt
each start) and is strictly best effort: a broker that cannot be reached is
logged and retried, and until it answers labels resolve from the row and the
defaults exactly as they did before this feed existed. It never blocks
publishing.
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiokafka import AIOKafkaConsumer

from .releasability import Label, ReleasabilityDeclaration

log = logging.getLogger("logistics_sim.declarations")

_RETRY_S = 15.0


def parse_label(value: bytes | None) -> Label | None:
    """Declaration record value -> Label. None for a tombstone or for a
    record that does not carry a usable originator_nation."""
    if not value:
        return None
    try:
        doc = json.loads(value)
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(doc, dict):
        return None
    nation = doc.get("originator_nation")
    if not isinstance(nation, str) or not nation.strip():
        return None
    releasable = doc.get("releasable_to") or []
    if not isinstance(releasable, list):
        return None
    return Label(nation.strip(), tuple(str(r) for r in releasable))


def apply_record(
    declaration: ReleasabilityDeclaration, key: bytes | None, value: bytes | None,
) -> None:
    """Fold one topic record into the declaration's arrival map. A
    tombstone (null value) removes the asset; an unparseable non-null record
    is skipped and leaves the prior declaration in place."""
    if not key:
        return
    asset_id = key.decode("utf-8", "replace")
    if value is None:
        declaration.set_arrival_declaration(asset_id, None)
        return
    label = parse_label(value)
    if label is None:
        log.warning("declaration for %s unparseable; ignored", asset_id)
        return
    declaration.set_arrival_declaration(asset_id, label)


async def run_declaration_consumer(
    brokers: str, topic: str, declaration: ReleasabilityDeclaration,
) -> None:
    while True:
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=brokers,
            group_id=None,
            auto_offset_reset="earliest",
            enable_auto_commit=False,
        )
        try:
            await consumer.start()
            log.info("declaration consumer started (brokers=%s topic=%s)",
                     brokers, topic)
            async for msg in consumer:
                apply_record(declaration, msg.key, msg.value)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("declaration consumer failed; labels fall to row/"
                          "defaults until it reconnects (retry in %.0fs)",
                          _RETRY_S)
        finally:
            try:
                await consumer.stop()
            except Exception:
                pass
        await asyncio.sleep(_RETRY_S)
