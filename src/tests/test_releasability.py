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
    # Declares an asset, and the message is matched. With neither, this test
    # passes on the zero-label refusal instead -- it would stay green with
    # the site_nation check deleted.
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [] }
""")
    with pytest.raises(ValueError, match="site_nation"):
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
    # The document declares SOMEBODY and the published asset is not them.
    # It used to declare nobody at all -- the state the loader now refuses --
    # in which every envelope is unlabelled and an assertion about absence
    # cannot fail.
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [] }
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
    # The document declares SOMEBODY and the published asset is not them.
    # It used to declare nobody at all -- the state the loader now refuses --
    # in which every envelope is unlabelled and an assertion about absence
    # cannot fail.
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets:
  "dis:1:1:1000": { originator_nation: ATL, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path)
    producer, stub = _make_producer(decl)
    await producer.publish_inventory(
        asset_id="dis:9:9:9999", asset_state=_asset_state(), elements=_one_element(),
    )
    envelope = json.loads(stub.sent[0][1])
    assert "originator_nation" not in envelope
    assert "releasable_to" not in envelope


# ---------------------------------------------------------------------------
# A declaration that labels nobody is refused
# ---------------------------------------------------------------------------

def test_authored_declaration_that_labels_nobody_raises(tmp_path: Path) -> None:
    """The compose defect, at the reader. A file that parses and names no
    one makes every envelope unlabelled -- a LEGAL answer, and therefore a
    run that passes while proving nothing."""
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets: {}
""")
    with pytest.raises(ValueError, match="labels nobody"):
        ReleasabilityDeclaration.load(path)


def test_zero_byte_declaration_raises(tmp_path: Path) -> None:
    """The defect exactly as it was made: a nested single-file bind mount
    into a writable parent creates a zero-byte file, which parses as a fleet
    of no assets."""
    path = tmp_path / "releasability.yaml"
    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="labels nobody"):
        ReleasabilityDeclaration.load(path)


def test_document_default_alone_is_not_refused(tmp_path: Path) -> None:
    """Naming no assets individually is not the same as labelling nobody:
    a document-wide default labels all of them."""
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: ATL
assets: {}
""")
    decl = ReleasabilityDeclaration.load(path)
    assert decl.asset_count == 0
    assert decl.label_for("dis:9:9:9999").originator_nation == "ATL"


def test_edge_defaults_alone_are_not_refused(tmp_path: Path) -> None:
    """Edge defaults label every asset of the edges they name, so a file
    with no rows and no document default has still declared something."""
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
default_originator_nation: null
assets: {}
""")
    decl = ReleasabilityDeclaration.load(
        path, edge_defaults={"edge-01": "ATL", "edge-02": "BDR"})
    assert decl.label_for("dis:9:9:9999", "edge-02").originator_nation == "BDR"
    assert decl.label_for("dis:9:9:9999", "edge-09") is None


def test_missing_file_is_not_the_same_as_labelling_nobody(tmp_path: Path) -> None:
    """A deployment may legitimately have no declaration. One that has
    AUTHORED a file has said it does, which is why only the second refuses."""
    decl = ReleasabilityDeclaration.load(tmp_path / "absent.yaml")
    assert decl.asset_count == 0
    assert decl.label_for("dis:9:9:9999") is None


# ---------------------------------------------------------------------------
# Full precedence: arrival declaration > row > edge default > document
# default > site_nation > none
# ---------------------------------------------------------------------------

_PRECEDENCE_DOC = _BASE_NATIONS + """
default_originator_nation: ATL
assets:
  "row-asset": { originator_nation: BDR, releasable_to: [ATL] }
"""
_EDGES = {"edge-01": "ATL", "edge-02": "BDR"}


def test_precedence_table(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _PRECEDENCE_DOC)
    decl = ReleasabilityDeclaration.load(path, site_nation="ATL", edge_defaults=_EDGES)
    decl.set_arrival_declaration("both", Label("ATL", ("BDR",)))
    decl.set_arrival_declaration("row-asset", Label("ATL", ()))

    # declared beats row
    assert decl.label_for("row-asset", "edge-02") == Label("ATL", ())
    # declared beats edge default
    assert decl.label_for("both", "edge-02") == Label("ATL", ("BDR",))
    # row beats edge default
    assert decl.set_arrival_declaration("row-asset", None) is None
    assert decl.label_for("row-asset", "edge-01") == Label("BDR", ("ATL",))
    # edge default beats document default (doc default is ATL; edge-02 is BDR)
    assert decl.label_for("other", "edge-02") == Label("BDR", ())
    # unknown edge / no edge falls through to document default
    assert decl.label_for("other", "edge-99") == Label("ATL", ())
    assert decl.label_for("other") == Label("ATL", ())
    c = decl.counts
    assert (c["arrival_declared"], c["declared"], c["edge_default"],
            c["document_default"]) == (2, 1, 1, 2)


def test_edge_default_beats_site_nation_and_site_beats_none(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
assets:
  "row-asset": { originator_nation: ATL, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path, site_nation="ATL", edge_defaults=_EDGES)
    assert decl.label_for("x", "edge-02") == Label("BDR", ())
    assert decl.label_for("x", "edge-99") == Label("ATL", ())
    assert decl.counts["site_default"] == 1
    bare = ReleasabilityDeclaration.load(path)
    assert bare.label_for("x", "edge-01") is None


def test_tombstone_retracts_arrival_declaration(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _PRECEDENCE_DOC)
    decl = ReleasabilityDeclaration.load(path)
    decl.set_arrival_declaration("row-asset", Label("ATL", ()))
    assert decl.label_for("row-asset") == Label("ATL", ())
    decl.set_arrival_declaration("row-asset", None)
    assert decl.label_for("row-asset") == Label("BDR", ("ATL",))


def test_bad_nation_in_edge_defaults_refused(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _PRECEDENCE_DOC)
    with pytest.raises(ValueError, match="edge-01"):
        ReleasabilityDeclaration.load(path, edge_defaults={"edge-01": "ZZZ"})


def test_publisher_passes_owning_edge(tmp_path: Path) -> None:
    path = _write_declaration(tmp_path, _BASE_NATIONS + """
assets:
  "row-asset": { originator_nation: ATL, releasable_to: [] }
""")
    decl = ReleasabilityDeclaration.load(path, edge_defaults=_EDGES)
    producer = HqProducer(
        "hq:9092", "t", declaration=decl,
        edge_brokers={"edge-01": "e1:9092", "edge-02": "e2:9092"},
        edge_of=lambda a: "edge-02" if a == "x" else None,
    )
    assert producer._label_fields("x") == {
        "originator_nation": "BDR", "releasable_to": [],
    }
    assert producer._label_fields("y") == {}


# ---------------------------------------------------------------------------
# Declaration feed (no broker): record -> arrival map
# ---------------------------------------------------------------------------

def test_declaration_feed_apply_and_tombstone(tmp_path: Path) -> None:
    from logistics_sim.declarations import apply_record

    decl = ReleasabilityDeclaration.load(
        _write_declaration(tmp_path, _PRECEDENCE_DOC))
    body = json.dumps({"originator_nation": "BDR", "releasable_to": ["ATL"]}).encode()
    apply_record(decl, b"a1", body)
    assert decl.label_for("a1") == Label("BDR", ("ATL",))
    # garbage leaves the prior declaration in place
    apply_record(decl, b"a1", b"not json")
    assert decl.label_for("a1") == Label("BDR", ("ATL",))
    # tombstone removes it; falls to document default
    apply_record(decl, b"a1", None)
    assert decl.label_for("a1") == Label("ATL", ())
    assert decl.arrival_count == 0
    # keyless record ignored
    apply_record(decl, None, body)
    assert decl.arrival_count == 0
