"""
platform_variant alias loading -- native -> canonical lookup.

The customer's upstream feeds carry PROPRIETARY platform_variant tokens
(e.g. "MRAD_Launcher", "MRAD_Radar"); the rest of OpenDDIL works with
CANONICAL tokens (e.g. "MRAD_Interceptor", "MRAD_Sensor"). The
authoritative mapping lives in `platform_variant_aliases.yaml`, which
is mounted into Restate-running services at `/ontology/`.

Today the alias mapping is applied AT INGRESS by the customer-overlay
Bloblang pipeline (see connect-proprietary, dynamic-mappings/
proprietary-mapping.yaml) -- BEFORE messages reach the projector +
postgres. The Kafka topic between the Bloblang stage and downstream
consumers (`raw-sensor-stream`) carries canonical names, and the
projector writes canonical names to postgres.

But `telemetry-latest-state` (which logistics-sim subscribes to) is
emitted by faust-edge from a SEPARATE code path that does NOT
canonicalize -- the wire there carries PROPRIETARY tokens. Without
this loader the sim's variant-matching against its config's canonical
`matches_platform_variants` list misses 100% of customer assets.

This module loads the alias map once at startup and flattens ALL
schemes (sim_a, proprietary, dis, ...) into a single
`{native: canonical}` dict. The sim's discovery loop canonicalizes
each incoming variant via `dict.get(variant, variant)` -- unknown
tokens pass through unchanged (treated as already-canonical), which
covers dev/docker-compose environments where the wire is already
aliased and the file may not be mounted.

Long-term: faust-edge should canonicalize in its producer step so
EVERY downstream consumer sees canonical tokens uniformly. Filed as
a follow-up; this module is the unblock for the sim today.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml

log = logging.getLogger("logistics_sim.aliases")

# Mount path used by the OSS chart's bundle-loader pattern (mirrors
# the fusion-service ontology mount in hub.yaml). Overridable via env
# so dev runs against a checkout path can override without code change.
DEFAULT_PATH = os.environ.get(
    "LOGISTICS_SIM_ALIAS_PATH",
    "/ontology/platform_variant_aliases.yaml",
)


def load_variant_aliases(
    path: str | os.PathLike = DEFAULT_PATH,
) -> dict[str, str]:
    """Read `platform_variant_aliases.yaml` and flatten to `{native: canonical}`.

    File schema (see ontology/platform_variant_aliases.yaml):

        aliases:
          proprietary:
            - native: "MRAD_Launcher"
              canonical: "MRAD_Interceptor"
            - native: "MRAD_Radar"
              canonical: "MRAD_Sensor"
            ...
          sim_a:
            - native: "AH-64E"
              canonical: "AH-64E"

    Returns a SINGLE flat dict merged across all schemes. If two schemes
    define the same `native` with different `canonical` values, the
    LAST one wins -- a degenerate case that would already be a bug in
    the alias file itself, but we log a warning when it happens so the
    operator notices.

    Missing or unreadable file -> empty dict + INFO log. The sim then
    degrades to canonical-only matching, which is the correct behavior
    in dev environments where the wire is already aliased upstream.

    Malformed YAML or missing top-level `aliases:` key -> empty dict +
    WARNING log. Better to start with an empty map than to crash and
    leave the maintainer demo dark.
    """
    p = Path(path)
    if not p.exists():
        log.info(
            "platform_variant_aliases.yaml not found at %s; sim will "
            "match canonical names only (assumes upstream aliasing "
            "already happened)", p,
        )
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning(
            "failed to load %s: %s; sim will match canonical names only",
            p, exc,
        )
        return {}

    aliases = (data or {}).get("aliases") or {}
    if not isinstance(aliases, dict):
        log.warning(
            "%s top-level `aliases:` key missing or malformed; "
            "sim will match canonical names only", p,
        )
        return {}

    out: dict[str, str] = {}
    for scheme_name, entries in aliases.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            native = entry.get("native")
            canonical = entry.get("canonical")
            if not native or not canonical:
                continue
            n, c = str(native), str(canonical)
            existing = out.get(n)
            if existing is not None and existing != c:
                log.warning(
                    "alias collision: native=%r mapped to both %r (existing) "
                    "and %r (in scheme %s); last value wins",
                    n, existing, c, scheme_name,
                )
            out[n] = c

    log.info("loaded %d platform-variant alias(es) from %s", len(out), p)
    return out
