"""
Tests for logistics_sim.config -- legacy back-compat for the per-tier
synthesis fields (2026-06-24 demo prep).

The 2026-06-24 change replaced the single `degraded_fraction` with six
per-tier fields (degraded_yellow_fraction, degraded_red_fraction,
fault_*, failed_*). Pre-existing customer-overlay configs may still
ship only the old `degraded_fraction`; the loader synthesizes per-tier
fields from it so those configs keep working.
"""
from __future__ import annotations

from pathlib import Path

from logistics_sim.config import SimConfig


def _write_minimal_config(
    tmp_path: Path,
    synthesis_yaml_keyed: dict,
) -> Path:
    """Build a minimal SimConfig YAML, splicing the caller's
    synthesis dict in directly. We construct the YAML by hand to
    avoid textwrap.indent footguns on multi-line dedented blocks."""
    lines = [
        "tick_interval_s: 30",
        "output_topic: asset-element-telemetry",
        "asset_profiles:",
        "  - name: mrad",
        "    matches_platform_variants: [\"MRAD_Sensor\"]",
        "    layers:",
        "      - name: RADAR UNIT",
        "        prefix: TR",
        "    faces:",
        "      - name: PRIMARY APERTURE",
        "        cols: 1",
        "        rows: 1",
        "    synthesis:",
    ]
    for k, v in synthesis_yaml_keyed.items():
        lines.append(f"      {k}: {v}")
    f = tmp_path / "test_config.yaml"
    f.write_text("\n".join(lines) + "\n")
    return f


def test_loader_honors_per_tier_fields(tmp_path: Path) -> None:
    """When per-tier fields are explicit in YAML, the loader uses them
    verbatim -- no override from the legacy degraded_fraction."""
    yaml = _write_minimal_config(tmp_path, {
        "health_nominal_min":        0.55,
        "health_nominal_max":        0.85,
        "degraded_yellow_fraction":  0.10,
        "degraded_red_fraction":     0.01,
        "fault_yellow_fraction":     0.20,
        "fault_red_fraction":        0.05,
        "failed_yellow_fraction":    0.30,
        "failed_red_fraction":       0.25,
        "degraded_fraction":         0.99,  # ignored when per-tier fields are present
    })
    cfg = SimConfig.load(yaml)
    s = cfg.profiles[0].synthesis
    assert s.degraded_yellow_fraction == 0.10
    assert s.degraded_red_fraction    == 0.01
    assert s.fault_yellow_fraction    == 0.20
    assert s.fault_red_fraction       == 0.05
    assert s.failed_yellow_fraction   == 0.30
    assert s.failed_red_fraction      == 0.25


def test_loader_back_compat_collapses_degraded_fraction(tmp_path: Path) -> None:
    """When per-tier fields are ABSENT, the loader synthesizes them
    from the legacy `degraded_fraction` -- split 60% yellow / 40% red
    across all degraded tiers. Pre-2026-06-24 customer configs keep
    working without YAML updates."""
    yaml = _write_minimal_config(tmp_path, {
        "health_nominal_min": 0.55,
        "health_nominal_max": 0.85,
        "degraded_fraction":  0.20,
    })
    cfg = SimConfig.load(yaml)
    s = cfg.profiles[0].synthesis
    assert s.degraded_fraction == 0.20
    # Each tier gets the SAME (0.6 yellow + 0.4 red) split from 0.20.
    expected_yellow = 0.20 * 0.6
    expected_red    = 0.20 * 0.4
    assert abs(s.degraded_yellow_fraction - expected_yellow) < 1e-9
    assert abs(s.degraded_red_fraction    - expected_red)    < 1e-9
    assert abs(s.fault_yellow_fraction    - expected_yellow) < 1e-9
    assert abs(s.fault_red_fraction       - expected_red)    < 1e-9
    assert abs(s.failed_yellow_fraction   - expected_yellow) < 1e-9
    assert abs(s.failed_red_fraction      - expected_red)    < 1e-9


def test_loader_partial_per_tier_uses_defaults_for_unset(tmp_path: Path) -> None:
    """If even ONE per-tier field is present, the loader takes the
    per-tier path (NOT the back-compat collapse) and fills unset
    fields with the demo-tuned defaults. Confirms the loader doesn't
    accidentally zero out unset fields."""
    yaml = _write_minimal_config(tmp_path, {
        "health_nominal_min":  0.55,
        "health_nominal_max":  0.85,
        "failed_red_fraction": 0.40,
    })
    cfg = SimConfig.load(yaml)
    s = cfg.profiles[0].synthesis
    # Explicitly set
    assert s.failed_red_fraction == 0.40
    # Defaults (from config.py per-tier defaults block)
    assert s.degraded_yellow_fraction == 0.15
    assert s.degraded_red_fraction    == 0.00
    assert s.fault_yellow_fraction    == 0.20
    assert s.fault_red_fraction       == 0.05
    assert s.failed_yellow_fraction   == 0.30


def _write_profile_with_suffix(
    tmp_path: Path, suffix: str | None,
) -> Path:
    """Build a minimal config with the given match_asset_id_suffix
    on the single profile. None -> field absent from YAML."""
    lines = [
        "tick_interval_s: 30",
        "output_topic: asset-element-telemetry",
        "asset_profiles:",
        "  - name: mrad",
        '    matches_platform_variants: ["MRAD_Sensor"]',
    ]
    if suffix is not None:
        lines.append(f'    match_asset_id_suffix: "{suffix}"')
    lines += [
        "    layers:",
        "      - name: RADAR UNIT",
        "        prefix: TR",
        "    faces:",
        "      - name: PRIMARY APERTURE",
        "        cols: 1",
        "        rows: 1",
        "    synthesis:",
        "      health_nominal_min: 0.55",
        "      health_nominal_max: 0.85",
        "      degraded_fraction: 0.15",
    ]
    f = tmp_path / "suffix_config.yaml"
    f.write_text("\n".join(lines) + "\n")
    return f


def test_match_asset_id_suffix_loaded_when_present(tmp_path: Path) -> None:
    yaml = _write_profile_with_suffix(tmp_path, "_Sensor")
    cfg = SimConfig.load(yaml)
    assert cfg.profiles[0].match_asset_id_suffix == "_Sensor"


def test_match_asset_id_suffix_defaults_to_empty_when_absent(tmp_path: Path) -> None:
    """No `match_asset_id_suffix:` line in YAML -> empty string ->
    suffix filter disabled in discovery (variant-only matching).
    Preserves the pre-2026-06-29 behavior for old configs."""
    yaml = _write_profile_with_suffix(tmp_path, None)
    cfg = SimConfig.load(yaml)
    assert cfg.profiles[0].match_asset_id_suffix == ""


def test_variant_suffix_map_emits_per_variant_suffix(tmp_path: Path) -> None:
    """SimConfig.variant_suffix_map keys = union of every profile's
    matches list; values = that profile's match_asset_id_suffix."""
    yaml = _write_profile_with_suffix(tmp_path, "_Sensor")
    cfg = SimConfig.load(yaml)
    assert cfg.variant_suffix_map == {"MRAD_Sensor": "_Sensor"}


def test_variant_suffix_map_empty_when_no_suffix_configured(tmp_path: Path) -> None:
    """No suffix configured -> map value is empty string for that
    variant -> discovery treats the entry as "no filter."""
    yaml = _write_profile_with_suffix(tmp_path, None)
    cfg = SimConfig.load(yaml)
    assert cfg.variant_suffix_map == {"MRAD_Sensor": ""}
