"""Tests for AssetRoster -- specifically the 2026-06-30 event-driven
tick affordance.

The event-driven path replaces the old "sleep tick_interval_s, then
maybe see the new state" behavior with "wait on (sleep | state-change
event), whichever fires first." The roster owns the asyncio.Event;
.upsert() sets it when the new state differs from the prior in any
tier-relevant field (the four AssetState fields that drive element_gen.
severity_tier).

These tests focus on the change-detection logic. The tick-loop
integration (asyncio.wait_for racing the event) isn't covered here --
that's a coroutine-time-flow test that would need patching, and the
behavior is small enough that the manual demo verification is sharper
feedback than a synthetic time-source test.
"""
from __future__ import annotations

import asyncio

import pytest

from logistics_sim.asset_discovery import AssetRoster
from logistics_sim.element_gen import AssetState


def _state(**overrides) -> AssetState:
    """Build an AssetState with sensible NOMINAL defaults; override any
    field for the case under test."""
    defaults = dict(
        platform_variant="MRAD_Sensor",
        power_state="POWER_STATE_ON",
        health_state="HEALTH_STATE_NOMINAL",
        actively_transmitting=True,
        actively_receiving=True,
    )
    defaults.update(overrides)
    return AssetState(**defaults)


# ---------------------------------------------------------------------------
# Lazy event creation
# ---------------------------------------------------------------------------

def test_upsert_without_event_access_does_not_construct_event() -> None:
    """Callers that never touch .changed_event must not pay the
    asyncio.Event construction cost (and must not fail under
    not-in-loop test conditions). The first call to .changed_event
    is what creates the event."""
    roster = AssetRoster()
    roster.upsert("asset-1", _state())
    # Internal handle is still None -- no event was created.
    assert roster._changed_event is None


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_new_asset_signals_change() -> None:
    """First sight of an asset is a discovery event -- the tick loop
    should wake to publish its first snapshot."""
    roster = AssetRoster()
    evt = roster.changed_event
    assert not evt.is_set()
    roster.upsert("asset-1", _state())
    assert evt.is_set()


@pytest.mark.asyncio
async def test_no_change_does_not_signal() -> None:
    """Re-upserting the same AssetState should NOT fire the event --
    every customer telemetry refresh shouldn't wake the tick loop
    when nothing meaningful changed."""
    roster = AssetRoster()
    _ = roster.changed_event  # force creation
    roster.upsert("asset-1", _state())
    roster.changed_event.clear()
    roster.upsert("asset-1", _state())
    assert not roster.changed_event.is_set()


@pytest.mark.asyncio
async def test_health_state_change_signals() -> None:
    roster = AssetRoster()
    _ = roster.changed_event
    roster.upsert("asset-1", _state(health_state="HEALTH_STATE_NOMINAL"))
    roster.changed_event.clear()
    roster.upsert("asset-1", _state(health_state="HEALTH_STATE_DEGRADED"))
    assert roster.changed_event.is_set()


@pytest.mark.asyncio
async def test_power_state_change_signals() -> None:
    roster = AssetRoster()
    _ = roster.changed_event
    roster.upsert("asset-1", _state(power_state="POWER_STATE_ON"))
    roster.changed_event.clear()
    roster.upsert("asset-1", _state(power_state="POWER_STATE_OFF"))
    assert roster.changed_event.is_set()


@pytest.mark.asyncio
async def test_tx_flag_change_signals() -> None:
    roster = AssetRoster()
    _ = roster.changed_event
    roster.upsert("asset-1", _state(actively_transmitting=True))
    roster.changed_event.clear()
    roster.upsert("asset-1", _state(actively_transmitting=False))
    assert roster.changed_event.is_set()


@pytest.mark.asyncio
async def test_rx_flag_change_signals() -> None:
    roster = AssetRoster()
    _ = roster.changed_event
    roster.upsert("asset-1", _state(actively_receiving=True))
    roster.changed_event.clear()
    roster.upsert("asset-1", _state(actively_receiving=False))
    assert roster.changed_event.is_set()


@pytest.mark.asyncio
async def test_non_tier_relevant_change_does_not_signal() -> None:
    """platform_variant doesn't affect element_gen output, so a change
    there shouldn't wake the tick loop. If we ever add a different
    profile for the same canonical variant family this might need
    revisiting, but today this is the right behavior."""
    roster = AssetRoster()
    _ = roster.changed_event
    roster.upsert("asset-1", _state(platform_variant="MRAD_Sensor"))
    roster.changed_event.clear()
    # Same tier fields, only variant differs.
    roster.upsert("asset-1", _state(platform_variant="MRAD2_radar"))
    assert not roster.changed_event.is_set()


# ---------------------------------------------------------------------------
# Burst coalescing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_burst_changes_coalesce_into_single_event_set() -> None:
    """10 simultaneous tier-relevant upserts should leave the event
    set exactly once -- the tick loop runs once with the roster's
    current state (all 10 changes reflected), then clears the event."""
    roster = AssetRoster()
    _ = roster.changed_event
    for i in range(10):
        roster.upsert(f"asset-{i}", _state(health_state="HEALTH_STATE_DEGRADED"))
    assert roster.changed_event.is_set()
    # Clearing once is sufficient regardless of N -- this is the
    # property the tick-loop coalesce contract depends on.
    roster.changed_event.clear()
    assert not roster.changed_event.is_set()
