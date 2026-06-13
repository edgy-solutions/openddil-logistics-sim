# openddil-logistics-sim

Per-element telemetry simulator for the maintainer-tier 3D drill-down
views. The upstream asset feeds (customer-overlay proprietary, DIS, future) don't
emit per-element radar / sub-component telemetry, but the maintainer
demo needs that level of detail to make the 3D drill-down interesting.
This sim augments the customer asset feed with synthetic per-element
health / temp / load values and honors the upstream operational state
so what the operator sees on the 3D drill-down stays in sync with what
the customer sim reports.

> Renamed from `openddil-mrad-sim` 2026-06-13 to reflect the wider
> framing: a logistics-domain sim that will cover MRAD, LTAMDS,
> Patriot, and future multi-element platforms via the multi-profile
> config.

## What's in the box

- **Multi-profile config** — one entry in `asset_profiles[]` per asset
  TYPE the sim knows. MRAD ships today; LTAMDS / Patriot / sonar /
  IR phased-arrays land as additional entries with no code change.
- **`operational_state` honoring** — `PowerState OFF / SHUTTING_DOWN /
  MAINTENANCE` or `HealthState DEGRADED / FAULT / FAILED` flip the
  per-element synthesis into the DEGRADED / CRITICAL band. The
  maintainer 3D drill-down lights up red in lockstep with the
  rolled-up state the operator already saw on the asset card.
- **`tx` / `rx` synchronization with the customer sim** — the
  `actively_transmitting` / `actively_receiving` bits from the
  customer feed's `OperationalState` propagate per-element on face
  T/R modules. Operator sees "asset's TX is off" mirrored as every
  face element's `tx_active=false`; internal support electronics
  (backplane, processor, MMIC chips) default to both true.
- **Per-asset determinism + independence** — seeded RNG per (asset_id,
  element_id, tick_bucket) so the same asset stays consistent
  across re-renders, different assets are independent, ticks
  produce visible movement. Locked in by tests.

## Architecture

```
┌──────────────────────────┐    ┌────────────────────────────────┐
│ edge-NN redpanda         │    │ AIOKafkaConsumer (per edge)    │
│   telemetry-latest-state │────▶                                │
└──────────────────────────┘    │  AssetRoster: in-mem mapping   │
                                │   asset_id -> AssetState       │
                                │  (platform_variant, power,     │
                                │   health, tx, rx)              │
                                │                                │
                                │  Filter: platform_variant ∈    │
                                │   any asset_profile's          │
                                │   matches_platform_variants    │
                                └─────────────┬──────────────────┘
                                              │
                                              ▼
                              ┌──────────────────────────────────┐
                              │  tick loop (every tick_interval) │
                              │  for asset in roster:            │
                              │    profile = lookup_by_variant   │
                              │    degraded = power|health|both- │
                              │               tx-rx-off          │
                              │    snapshot = gen_per_element(   │
                              │       degraded, asset.tx,        │
                              │       asset.rx, profile.layers/  │
                              │       faces/synthesis)           │
                              │    publish to HQ                 │
                              └──────────────────┬───────────────┘
                                                 │
                                                 ▼
                                  ┌────────────────────────────┐
                                  │ HQ redpanda                │
                                  │   asset-element-telemetry  │
                                  └────────────┬───────────────┘
                                               │
                                  (projector -> asset_element_telemetry
                                   postgres -> ElectricSQL ->
                                   SensorArrayView liveTelemetry)
```

## Config

YAML at `LOGISTICS_SIM_CONFIG_PATH` (default
`/etc/openddil/logistics-sim/config.yaml`). A baked default at
`/app/config/default.yaml` lets the image boot without a mounted
ConfigMap.

### Top-level keys

| Key | Purpose |
|---|---|
| `tick_interval_s` | Seconds between snapshot ticks |
| `output_topic` | Kafka topic on HQ |
| `degraded_power_states[]` | `PowerState` enum names that count as degraded |
| `degraded_health_states[]` | `HealthState` enum names that count as degraded |
| `asset_profiles[]` | One entry per asset TYPE — see below |

### Per-profile keys

| Key | Purpose |
|---|---|
| `name` | Identifier carried on every published envelope |
| `matches_platform_variants[]` | `platform_variant` strings this profile handles |
| `layers[]` | Per-depth layout (name, prefix, cols, rows) |
| `faces[]` | Depth-0 face cardinality (name, cols, rows) |
| `synthesis.*` | Value range + drift knobs (per-asset-type — radar runs hotter than launcher controllers) |

### Kafka wiring (env)

| Env | Default |
|---|---|
| `LOGISTICS_SIM_EDGE_CLUSTERS` | `edge-01=openddil-redpanda-edge-01:9092,...` |
| `LOGISTICS_SIM_HQ_BROKERS` | `openddil-redpanda-hq:19092` |
| `LOGISTICS_SIM_INPUT_TOPIC` | `telemetry-latest-state` |
| `LOGISTICS_SIM_CONSUMER_GROUP_PREFIX` | `logistics-sim` |
| `LOG_LEVEL` | `INFO` |

## Element id format

Identical to the frontend `SensorArrayView` for matching `liveTelemetry`
lookup:
- Depth 0 (face elements): `<prefix>-<face-no-whitespace>-<i>-<j>`
- Depth 1..N: `<prefix>-<i>-<j>`

## Output payload

One Kafka record per asset per tick, JSON, keyed by `asset_id`:

```json
{
  "asset_id": "demo:mrad-sensor-001",
  "platform_variant": "MRAD2_radar",
  "profile_name": "mrad",
  "observed_at_ns": 1781340000000000000,
  "operational": {
    "power_state": "POWER_STATE_ON",
    "health_state": "HEALTH_STATE_NOMINAL",
    "actively_transmitting": true,
    "actively_receiving": true,
    "degraded": false
  },
  "elements": [
    {
      "element_id": "TR-PRIMARYAPERTURE-0-0",
      "layer_depth": 0,
      "layer_name": "RADAR UNIT",
      "health": 0.62,
      "temp_c": 41.83,
      "load_pct": 53.4,
      "tx_active": true,
      "rx_active": true
    },
    ...
  ]
}
```

The `operational` block surfaces what the customer sim reported about
the asset alongside the per-element data, so a downstream consumer
(maintainer UI banner, future RTI exporter) can render asset-level
status without re-deriving from per-element bits.

## Adding a new profile (e.g. LTAMDS)

1. Append an entry to `asset_profiles[]` matching the frontend
   `LTAMDS_CONFIG` (or whichever `SensorArrayConfig` the asset uses).
2. List its `platform_variant` strings under
   `matches_platform_variants`.
3. Tune `synthesis.*` for its temp / load ranges if they differ
   meaningfully from the existing profile.
4. No code change needed — the discovery loop's
   `all_matched_variants` rebuild picks it up on next pod restart.

## Tests

```bash
PYTHONPATH=src pytest src/tests/ -q
```

16 tests cover: cardinality, frontend-id-format match, per-(asset,
tick) determinism, per-asset independence, value bounds, tick
advancement, degraded-band lift, operational_state plumbing
(power, health, both-tx-rx-off mismatch), tx/rx propagation per
face element, internal-layer defaults, and unspecified-state
defaults.

Run them before every change to `element_gen.py`, `config.py`,
or `asset_discovery.py` -- those are the contracts that keep the
frontend lookup + customer-sim sync working.
