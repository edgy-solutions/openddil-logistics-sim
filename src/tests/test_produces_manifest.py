"""The code must produce exactly the topics produces.yaml lists.

AST scan of src/logistics_sim/*.py, the package the image runs (CMD `logistics-sim`, the one chart
workload `logistics-sim`). Not scanned: src/tests/ (the image does not run it). There are no
operator tools or scripts in this repo.

Producer call sites: `.send(`, `.send_and_wait(`, `.produce(`. There is no publish wrapper:
publisher.HqProducer ends every path in `send_and_wait(`. All four sites take their topic from an
attribute (`self._topic`, `self._inventory_topic`, `cfg.topic` x2) set elsewhere from config/default.yaml
or an env override, so each carries `# produces: <manifest entry>`. Any other site must resolve statically
(literal, module constant, os.environ.get / os.getenv) or carry the same annotation.
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import produces_manifest as pm  # noqa: E402

MANIFEST = pm.load(ROOT / "produces.yaml")
METHODS = {"send", "send_and_wait", "produce"}
WRAPPERS: set[str] = set()
ANNOTATION = re.compile(r"#\s*produces:\s*(.+?)\s*$")
ENV = {}  # fixture env: every code default applies


def _env_template(call):
    """os.environ.get("X", "d") / os.getenv("X", "d") -> '${X:-d}', else None."""
    f = call.func
    ok = isinstance(f, ast.Attribute) and (
        (f.attr == "getenv" and isinstance(f.value, ast.Name) and f.value.id == "os")
        or (f.attr == "get" and isinstance(f.value, ast.Attribute) and f.value.attr == "environ"))
    if not ok or not call.args or not isinstance(call.args[0], ast.Constant):
        return None
    var = call.args[0].value
    if len(call.args) > 1 and isinstance(call.args[1], ast.Constant) and isinstance(call.args[1].value, str):
        return f"${{{var}:-{call.args[1].value}}}"
    return f"${{{var}}}"


def _scan(root: Path = ROOT / "src" / "logistics_sim"):
    templates, bad, sites = set(), [], {}
    for path in sorted(root.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        lines = src.splitlines()
        tree = ast.parse(src)
        consts = {}
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                if isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                    consts[n.targets[0].id] = n.value.value
                elif isinstance(n.value, ast.Call) and _env_template(n.value):
                    consts[n.targets[0].id] = _env_template(n.value)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not ((isinstance(f, ast.Attribute) and f.attr in METHODS)
                    or (isinstance(f, ast.Name) and f.id in WRAPPERS)
                    or (isinstance(f, ast.Attribute) and f.attr in WRAPPERS)):
                continue
            sites[path.name] = sites.get(path.name, 0) + 1
            span = lines[max(node.lineno - 2, 0):node.end_lineno]
            note = next((m.group(1).strip("\"'") for m in map(ANNOTATION.search, span) if m), None)
            if note:
                templates.add(note)
                continue
            kw = {k.arg: k.value for k in node.keywords}
            arg = kw.get("topic") or (node.args[0] if node.args else None)
            got = None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                got = arg.value
            elif isinstance(arg, ast.Name):
                got = consts.get(arg.id)
            elif isinstance(arg, ast.Call):
                got = _env_template(arg)
            if got:
                templates.add(got)
            else:
                bad.append(f"{path.name}:{node.lineno}")
    return templates, bad, sites


def test_code_produces_exactly_the_manifest():
    templates, bad, sites = _scan()
    assert sites, "no producer call sites found: the scan is not looking at the code"
    assert not bad, ("unresolvable producer call sites (annotate with '# produces: <entry>'): "
                     + ", ".join(bad))
    got = {pm.expand(t, ENV) for t in templates}
    declared = {pm.expand(t, ENV) for w in MANIFEST.values() for t in w["topics"]}
    msg = [f"manifest lists a topic the code does not produce: {t}" for t in sorted(declared - got)]
    msg += [f"code produces a topic the manifest does not list: {t}" for t in sorted(got - declared)]
    assert not msg, "\n".join(msg)


def test_manifest_is_exactly_the_logistics_sim_image():
    assert set(MANIFEST) == {"logistics-sim"}
    assert {w["image"] for w in MANIFEST.values()} == {"openddil/logistics-sim"}


# ---- the annotated sites against the real config loader -------------------------------------------
# Every send site reads its topic from an attribute, so the annotations alone cannot notice a renamed
# topic. These tests load config/default.yaml through SimConfig.load (the image's path) under a cleared
# env and compare what the attributes hold with the manifest and with each annotation.
SITE_FUNCS = {  # enclosing function -> the topic attribute its send site reads
    "publish_snapshot": "self._topic",
    "publish_inventory": "self._inventory_topic",
    "publish_fielding": "cfg.topic",
    "publish_parts_availability": "cfg.topic",
}


def _annotated_sites():
    path = ROOT / "src" / "logistics_sim" / "publisher.py"
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    tree = ast.parse(src)
    out, default_inv = {}, None
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if fn.name == "__init__":
                a = fn.args
                names = [x.arg for x in a.args]
                if "inventory_topic" in names:
                    default_inv = ast.literal_eval(a.defaults[names.index("inventory_topic") - len(names)])
            if fn.name in SITE_FUNCS:
                for n in ast.walk(fn):
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in METHODS:
                        span = lines[max(n.lineno - 2, 0):n.end_lineno]
                        note = next((m.group(1).strip("\"'") for m in map(ANNOTATION.search, span) if m), None)
                        out[fn.name] = (ast.unparse(n.args[0]), note)
    return out, default_inv


def _load_cfg(monkeypatch, config_path=ROOT / "config" / "default.yaml"):
    import os
    for k in [k for k in os.environ if k.startswith("LOGISTICS_SIM_")]:
        monkeypatch.delenv(k)
    sys.path.insert(0, str(ROOT / "src"))
    from logistics_sim.config import SimConfig
    return SimConfig.load(config_path)


def _config_topics(cfg, default_inv):
    return {
        "publish_snapshot": cfg.output_topic,  # main.py passes cfg.output_topic to HqProducer
        "publish_inventory": default_inv,      # no caller passes inventory_topic
        "publish_fielding": cfg.fielding.topic,
        "publish_parts_availability": cfg.parts_availability.topic,
    }


def test_config_topics_equal_the_manifest(monkeypatch):
    sites, default_inv = _annotated_sites()
    assert set(sites) == set(SITE_FUNCS), f"send sites moved: {sorted(set(SITE_FUNCS) ^ set(sites))}"
    got = set(_config_topics(_load_cfg(monkeypatch), default_inv).values())
    declared = {pm.expand(t, ENV) for w in MANIFEST.values() for t in w["topics"]}
    msg = [f"manifest lists a topic the loaded config does not produce: {t}" for t in sorted(declared - got)]
    msg += [f"loaded config produces a topic the manifest does not list: {t}" for t in sorted(got - declared)]
    assert not msg, "\n".join(msg)


def test_annotations_equal_the_loaded_config(monkeypatch):
    sites, default_inv = _annotated_sites()
    topics = _config_topics(_load_cfg(monkeypatch), default_inv)
    bad = [f"{fn}: annotation {note!r} expands to {pm.expand(note, ENV) if note else None!r}, "
           f"config holds {topics[fn]!r}"
           for fn, (_, note) in sorted(sites.items())
           if not note or pm.expand(note, ENV) != topics[fn]]
    assert not bad, "\n".join(bad)


def test_the_fielding_gate_is_the_code_gate():
    """produces.yaml's `when:` for asset-fielding must be the code's own gate: main.py reads the config from
    LOGISTICS_SIM_CONFIG_PATH and starts _fielding_loop only under `if cfg.fielding.records:` (that the loader
    fills records from the file's fielding.records is test_fielding.py's test_loop_gate_is_on_records)."""
    assert MANIFEST["logistics-sim"].get("when") == {
        "asset-fielding": "@config:LOGISTICS_SIM_CONFIG_PATH:fielding.records"}
    tree = ast.parse((ROOT / "src" / "logistics_sim" / "main.py").read_text(encoding="utf-8"))
    assert any(isinstance(n, ast.Constant) and n.value == "LOGISTICS_SIM_CONFIG_PATH" for n in ast.walk(tree))
    parent = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_fielding_loop"]
    assert calls, "main.py no longer calls _fielding_loop"
    for call in calls:
        node = call
        while node in parent and not isinstance(node, ast.If):
            node = parent[node]
        assert isinstance(node, ast.If) and ast.unparse(node.test) == "cfg.fielding.records", (
            f"main.py:{call.lineno}: _fielding_loop is not gated on cfg.fielding.records")
