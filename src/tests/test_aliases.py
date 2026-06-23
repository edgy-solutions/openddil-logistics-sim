"""
Tests for logistics_sim.aliases.load_variant_aliases.

The loader is the sim's bridge between upstream proprietary variant
tokens and the canonical tokens its profile-matching logic uses. The
discovery loop is otherwise correct -- if this loader returns the
wrong map, no asset gets rostered. Test the file-shape variations
the customer overlay can actually produce.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

from logistics_sim.aliases import load_variant_aliases


def test_loads_proprietary_and_sim_a_schemes(tmp_path: Path) -> None:
    f = tmp_path / "platform_variant_aliases.yaml"
    f.write_text(textwrap.dedent("""
        aliases:
          sim_a:
            - native: "M1A2-SEPv3"
              canonical: "M1A2-SEPv3"
            - native: "M1A2 SEPv3"
              canonical: "M1A2-SEPv3"
          proprietary:
            - native: "MRAD_Launcher"
              canonical: "MRAD_Interceptor"
            - native: "MRAD_Radar"
              canonical: "MRAD_Sensor"
    """).strip())
    out = load_variant_aliases(f)
    # Flattened across schemes.
    assert out == {
        "M1A2-SEPv3": "M1A2-SEPv3",
        "M1A2 SEPv3": "M1A2-SEPv3",
        "MRAD_Launcher": "MRAD_Interceptor",
        "MRAD_Radar": "MRAD_Sensor",
    }


def test_missing_file_returns_empty_dict(tmp_path: Path) -> None:
    """Dev/docker-compose path -- no file mounted, sim degrades to
    canonical-only matching. MUST NOT raise."""
    out = load_variant_aliases(tmp_path / "does-not-exist.yaml")
    assert out == {}


def test_malformed_yaml_returns_empty_dict(tmp_path: Path) -> None:
    """A misedited alias file shouldn't blow up the sim startup."""
    f = tmp_path / "broken.yaml"
    f.write_text("aliases:\n  - this is not\n    a valid: structure: :::")
    out = load_variant_aliases(f)
    assert out == {}


def test_missing_aliases_key_returns_empty_dict(tmp_path: Path) -> None:
    """File exists, parses as YAML, but has no top-level aliases key."""
    f = tmp_path / "wrong_shape.yaml"
    f.write_text("something_else:\n  - foo: bar\n")
    out = load_variant_aliases(f)
    assert out == {}


def test_empty_aliases_section_returns_empty_dict(tmp_path: Path) -> None:
    f = tmp_path / "empty.yaml"
    f.write_text("aliases: {}\n")
    out = load_variant_aliases(f)
    assert out == {}


def test_entries_with_missing_native_or_canonical_are_skipped(
    tmp_path: Path,
) -> None:
    """Half-filled entries are silently skipped, valid entries still load."""
    f = tmp_path / "partial.yaml"
    f.write_text(textwrap.dedent("""
        aliases:
          proprietary:
            - native: "MRAD_Launcher"
              canonical: "MRAD_Interceptor"
            - native: "OnlyNative"
            - canonical: "OnlyCanonical"
            - {}
            - native: "MRAD_Radar"
              canonical: "MRAD_Sensor"
    """).strip())
    out = load_variant_aliases(f)
    assert out == {
        "MRAD_Launcher": "MRAD_Interceptor",
        "MRAD_Radar": "MRAD_Sensor",
    }


def test_collision_across_schemes_last_wins(
    tmp_path: Path, caplog,
) -> None:
    """Pathological alias-file authoring error -- the sim survives, logs
    a warning, picks the last value. Document the behavior so an
    operator who sees this warning knows what to expect."""
    f = tmp_path / "collision.yaml"
    f.write_text(textwrap.dedent("""
        aliases:
          sim_a:
            - native: "DUAL"
              canonical: "from-sim-a"
          proprietary:
            - native: "DUAL"
              canonical: "from-proprietary"
    """).strip())
    import logging
    with caplog.at_level(logging.WARNING, logger="logistics_sim.aliases"):
        out = load_variant_aliases(f)
    assert out == {"DUAL": "from-proprietary"}
    assert any("alias collision" in r.message for r in caplog.records)


def test_passthrough_aliases_load_correctly(tmp_path: Path) -> None:
    """Customer overlay uses passthrough (native==canonical) entries
    extensively -- e.g. MRAD_Sensor -> MRAD_Sensor. These MUST load,
    not be filtered as redundant."""
    f = tmp_path / "passthrough.yaml"
    f.write_text(textwrap.dedent("""
        aliases:
          proprietary:
            - native: "MRAD_Sensor"
              canonical: "MRAD_Sensor"
            - native: "MRAD_Interceptor"
              canonical: "MRAD_Interceptor"
    """).strip())
    out = load_variant_aliases(f)
    assert out == {
        "MRAD_Sensor": "MRAD_Sensor",
        "MRAD_Interceptor": "MRAD_Interceptor",
    }
