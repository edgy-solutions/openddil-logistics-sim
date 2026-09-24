"""
Tests for logistics_sim.releasability (ADR-0029 §3 / Arc 2 Slice 1) and
its wiring into HqProducer's two envelope shapes.

Precedence under test: declared asset > document default
(`default_originator_nation`) > deployment `site_nation` > no label at
all. See releasability.py's module docstring and
openddil-demo/ontology/releasability.yaml's header for the rules.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from logistics_sim.element_gen import AssetState, ElementTelemetry
from logistics_sim.publisher import HqProducer
from logistics_sim.releasability import Label, ReleasabilityDeclaration


def _write_declaration(tmp_path: Path, body: str) -> Path:
    f = tmp_path / "releasability.yaml"
    f.write_text(body)
    return f


_BASE_NATIONS = """\
version: 1
nations:
  ATL: {name: Atlantia}
  BDR: {name: Borduria}
"""


# ---------------------------------------------------------------------------
# ReleasabilityDeclaration.load / label_for precedence
# ---------------------------------------------------------------------------

def test_declared_asset_with_releasable_to_round_trips(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [BDR] }
""")
    decl = ReleasabilityDeclaration.load(path)
    label = decl.label_for("dis:1:1:1000")
    assert label == Label("ATL", ("BDR",))
    assert decl.counts["declared"] == 1


def test_declared_asset_with_empty_releasable_to_is_labelled_not_absent(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1001": { originator_nation: ATL, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path)
    label = decl.label_for("dis:1:1:1001")
    assert label is not None
    assert label.originator_nation == "ATL"
    assert label.releasable_to == ()


def test_undeclared_asset_no_default_no_site_nation_yields_no_label(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path)
    assert decl.label_for("dis:9:9:9999") is None
    assert decl.counts["unlabelled"] == 1


def test_undeclared_asset_with_site_nation_yields_site_default(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path, site_nation="BDR")
    label = decl.label_for("dis:9:9:9999")
    assert label == Label("BDR", ())
    assert decl.counts["site_default"] == 1


def test_document_default_beats_site_nation(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: ATL
assets: {}
""")
    decl = ReleasabilityDeclaration.load(path, site_nation="BDR")
    label = decl.label_for("dis:9:9:9999")
    assert label == Label("ATL", ())
    assert decl.counts["document_default"] == 1


def test_declared_asset_beats_both_defaults(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: ATL
assets:
  "dis:2:1:1000": { originator_nation: BDR, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path, site_nation="ATL")
    label = decl.label_for("dis:2:1:1000")
    assert label == Label("BDR", ())
    assert decl.counts["declared"] == 1


def test_unknown_nation_code_in_asset_row_raises_naming_the_asset(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:9:9:9999": { originator_nation: XYZ, releasable_to: [] }
""")
    with pytest.raises(ValueError) as exc_info:
        ReleasabilityDeclaration.load(path)
    message = str(exc_info.value)
    assert "dis:9:9:9999" in message
    assert "XYZ" in message


def test_unknown_nation_code_in_releasable_to_raises(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [ZZZ] }
""")
    with pytest.raises(ValueError) as exc_info:
        ReleasabilityDeclaration.load(path)
    message = str(exc_info.value)
    assert "dis:1:1:1000" in message
    assert "ZZZ" in message


def test_unknown_default_originator_nation_raises(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: ZZZ
assets: {}
""")
    with pytest.raises(ValueError):
        ReleasabilityDeclaration.load(path)


def test_unknown_site_nation_raises(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets: {}
""")
    with pytest.raises(ValueError):
        ReleasabilityDeclaration.load(path, site_nation="ZZZ")


def test_missing_file_yields_unavailable_but_still_honours_site_nation(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    decl = ReleasabilityDeclaration.load(missing, site_nation="ATL")
    assert decl.asset_count == 0
    label = decl.label_for("dis:9:9:9999")
    assert label == Label("ATL", ())


def test_missing_file_without_site_nation_yields_no_label(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    decl = ReleasabilityDeclaration.load(missing)
    assert decl.label_for("dis:9:9:9999") is None


def test_unavailable_classmethod_direct() -> None:
    decl = ReleasabilityDeclaration.unavailable("test reason")
    assert decl.asset_count == 0
    assert decl.label_for("anything") is None


# ---------------------------------------------------------------------------
# Envelope-level wiring: HqProducer.publish_snapshot / publish_inventory
# ---------------------------------------------------------------------------

class _StubProducer:
    """Minimal stand-in for AIOKafkaProducer.send_and_wait -- records
    every (topic, value, key) call so tests can decode the JSON."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes, bytes]] = []

    async def send_and_wait(self, topic, value, key=None) -> None:
        self.sent.append((topic, value, key))


def _make_producer(declaration: ReleasabilityDeclaration | None) -> tuple[HqProducer, _StubProducer]:
    producer = HqProducer("brokers:9092", "asset-element-telemetry", declaration=declaration)
    stub = _StubProducer()
    producer._producer = stub  # type: ignore[attr-defined]
    return producer, stub


def _asset_state() -> AssetState:
    return AssetState(platform_variant="MRAD_Sensor")


def _one_element() -> list[ElementTelemetry]:
    return [ElementTelemetry(
        element_id="TR-0-0", layer_depth=0, layer_name="RADAR UNIT",
        health=0.5, temp_c=40.0, load_pct=50.0,
    )]


@pytest.mark.asyncio
async def test_snapshot_envelope_carries_label_when_declared(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [BDR] }
""")
    decl = ReleasabilityDeclaration.load(path)
    producer, stub = _make_producer(decl)
    await producer.publish_snapshot(
        asset_id="dis:1:1:1000", asset_state=_asset_state(), profile_name="mrad",
        elements=_one_element(), degraded=False,
    )
    envelope = json.loads(stub.sent[0][1])
    assert envelope["originator_nation"] == "ATL"
    assert envelope["releasable_to"] == ["BDR"]


@pytest.mark.asyncio
async def test_snapshot_envelope_omits_label_keys_when_unlabelled(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets: {}
""")
    decl = ReleasabilityDeclaration.load(path)
    producer, stub = _make_producer(decl)
    await producer.publish_snapshot(
        asset_id="dis:9:9:9999", asset_state=_asset_state(), profile_name="mrad",
        elements=_one_element(), degraded=False,
    )
    envelope = json.loads(stub.sent[0][1])
    assert "originator_nation" not in envelope
    assert "releasable_to" not in envelope


@pytest.mark.asyncio
async def test_snapshot_envelope_no_label_keys_when_no_declaration_wired() -> None:
    producer, stub = _make_producer(None)
    await producer.publish_snapshot(
        asset_id="dis:9:9:9999", asset_state=_asset_state(), profile_name="mrad",
        elements=_one_element(), degraded=False,
    )
    envelope = json.loads(stub.sent[0][1])
    assert "originator_nation" not in envelope
    assert "releasable_to" not in envelope


@pytest.mark.asyncio
async def test_inventory_envelope_carries_label_when_declared(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1001": { originator_nation: ATL, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path)
    producer, stub = _make_producer(decl)
    await producer.publish_inventory(
        asset_id="dis:1:1:1001", asset_state=_asset_state(), elements=_one_element(),
    )
    envelope = json.loads(stub.sent[0][1])
    assert envelope["originator_nation"] == "ATL"
    assert envelope["releasable_to"] == []


@pytest.mark.asyncio
async def test_inventory_envelope_omits_label_keys_when_unlabelled(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets: {}
""")
    decl = ReleasabilityDeclaration.load(path)
    producer, stub = _make_producer(decl)
    await producer.publish_inventory(
        asset_id="dis:9:9:9999", asset_state=_asset_state(), elements=_one_element(),
    )
    envelope = json.loads(stub.sent[0][1])
    assert "originator_nation" not in envelope
    assert "releasable_to" not in envelope
