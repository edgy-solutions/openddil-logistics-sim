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

import pytest

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


def _write_profile_with_key(
    tmp_path: Path, key: str | None, value: str = "ASSET_SUBSYSTEM_SENSOR",
) -> Path:
    """Build a minimal config with `key: value` on the single profile.
    None -> no filter key in YAML."""
    lines = [
        "tick_interval_s: 30",
        "output_topic: asset-element-telemetry",
        "asset_profiles:",
        "  - name: mrad",
        '    matches_platform_variants: ["MRAD_Sensor"]',
    ]
    if key is not None:
        lines.append(f'    {key}: "{value}"')
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
    f = tmp_path / "subsystem_config.yaml"
    f.write_text("\n".join(lines) + "\n")
    return f


def test_match_subsystem_loaded_when_present(tmp_path: Path) -> None:
    yaml = _write_profile_with_key(tmp_path, "match_subsystem")
    cfg = SimConfig.load(yaml)
    assert cfg.profiles[0].match_subsystem == "ASSET_SUBSYSTEM_SENSOR"


def test_match_subsystem_defaults_to_empty_when_absent(tmp_path: Path) -> None:
    """No `match_subsystem:` line in YAML -> empty string -> filter
    disabled in discovery (variant-only matching)."""
    yaml = _write_profile_with_key(tmp_path, None)
    cfg = SimConfig.load(yaml)
    assert cfg.profiles[0].match_subsystem == ""


def test_old_suffix_key_fails_load(tmp_path: Path) -> None:
    """The retired asset_id-suffix key must fail loudly, naming the
    replacement, rather than be silently ignored."""
    yaml = _write_profile_with_key(tmp_path, "match_asset_id_suffix", "_Sensor")
    with pytest.raises(ValueError, match="match_subsystem"):
        SimConfig.load(yaml)


def test_variant_subsystem_map_emits_per_variant_subsystem(tmp_path: Path) -> None:
    """SimConfig.variant_subsystem_map keys = union of every profile's
    matches list; values = that profile's match_subsystem."""
    yaml = _write_profile_with_key(tmp_path, "match_subsystem")
    cfg = SimConfig.load(yaml)
    assert cfg.variant_subsystem_map == {"MRAD_Sensor": "ASSET_SUBSYSTEM_SENSOR"}


def test_variant_subsystem_map_empty_when_none_configured(tmp_path: Path) -> None:
    """No subsystem configured -> map value is empty string for that
    variant -> discovery treats the entry as "no filter."""
    yaml = _write_profile_with_key(tmp_path, None)
    cfg = SimConfig.load(yaml)
    assert cfg.variant_subsystem_map == {"MRAD_Sensor": ""}


# ---------------------------------------------------------------------------
# releasability_path / site_nation -- deployment identity, env wins over
# YAML (same convention as the Kafka wiring fields).
# ---------------------------------------------------------------------------

def test_releasability_defaults_when_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOGISTICS_SIM_RELEASABILITY_PATH", raising=False)
    monkeypatch.delenv("LOGISTICS_SIM_SITE_NATION", raising=False)
    yaml = _write_profile_with_key(tmp_path, None)
    cfg = SimConfig.load(yaml)
    assert cfg.releasability_path == "/ontology/releasability.yaml"
    assert cfg.site_nation == ""


def test_releasability_read_from_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LOGISTICS_SIM_RELEASABILITY_PATH", raising=False)
    monkeypatch.delenv("LOGISTICS_SIM_SITE_NATION", raising=False)
    lines = [
        "tick_interval_s: 30",
        "output_topic: asset-element-telemetry",
        "releasability_path: /custom/releasability.yaml",
        "site_nation: ATL",
        "asset_profiles:",
        "  - name: mrad",
        '    matches_platform_variants: ["MRAD_Sensor"]',
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
    f = tmp_path / "releasability_config.yaml"
    f.write_text("\n".join(lines) + "\n")
    cfg = SimConfig.load(f)
    assert cfg.releasability_path == "/custom/releasability.yaml"
    assert cfg.site_nation == "ATL"


def test_releasability_env_wins_over_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    lines = [
        "tick_interval_s: 30",
        "output_topic: asset-element-telemetry",
        "releasability_path: /custom/releasability.yaml",
        "site_nation: ATL",
        "asset_profiles:",
        "  - name: mrad",
        '    matches_platform_variants: ["MRAD_Sensor"]',
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
    f = tmp_path / "releasability_env_config.yaml"
    f.write_text("\n".join(lines) + "\n")
    monkeypatch.setenv("LOGISTICS_SIM_RELEASABILITY_PATH", "/env/releasability.yaml")
    monkeypatch.setenv("LOGISTICS_SIM_SITE_NATION", "BDR")
    cfg = SimConfig.load(f)
    assert cfg.releasability_path == "/env/releasability.yaml"
    assert cfg.site_nation == "BDR"
