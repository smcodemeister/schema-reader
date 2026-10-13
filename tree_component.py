"""
tree_component.py — the original Schema Reader object tree as a Streamlit
custom component.

The tree itself (tree_frontend/index.html) is the windowed, lazily-expanded
tree from schema-reader.html. Python hands it the model once per upload as a
JSON file served next to the component, so a click only sends a few small
arguments back and forth rather than the whole schema.

The component returns events: {"seq", "action", ...}. Actions:
  open      {i, tab}     open object i (tab is a detail-section name or None)
  fav       {i}          toggle favourite
  diagram   {i}          show object i on the diagram
  search    {q, deep}    run a code-aware search in Python (results come back
                         through the `remote` argument on the next run)
  rename    {key, name}  rename an inferred domain
  loose     {}           show statements not tied to an object
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import streamlit.components.v1 as components

from schema_engine import Model, is_generated

_DIR = Path(__file__).parent / "tree_frontend"
_DATA = _DIR / "data"
_component = components.declare_component("schema_tree", path=str(_DIR))

J = "\u0001"


def _payload(M: Model) -> dict:
    ix = {o.id: i for i, o in enumerate(M.list)}
    files = [f["name"] for f in M.files]
    fix = {n: i for i, n in enumerate(files)}
    doms = list(M.domain_list)
    dix = {d: i for i, d in enumerate(doms)}
    objs, stats, srch, uses, used_by, det = [], [], [], [], [], {}
    for i, o in enumerate(M.list):
        objs.append([o.name, o.schema, o.type, M.group_ix.get(o.group, 0), fix.get(o.file, 0),
                     dix.get(M.domain_of(o), 0), 1 if is_generated(o) else 0])
        s = M.st(o.id)
        stats.append([s["cols"], s["fks"], s["in_fk"], s["idx"], s["trg"], s["cons"], s["refs"], s["ref_by"]])
        srch.append([J.join(c["name"] for c in o.columns).lower(),
                     J.join(dict.fromkeys(c["type"] for c in o.columns if c["type"])).lower(),
                     J.join(c["name"] for c in o.constraints if c["name"]).lower(),
                     J.join(x["name"] for x in o.indexes if x["name"]).lower(),
                     J.join((f["ref_schema"] + "." if f["ref_schema"] else "") + f["ref_table"] for f in o.fks).lower()])
        uses.append([ix[x] for x in M.refs(o.id) if x in ix])
        used_by.append([ix[x] for x in M.ref_by(o.id) if x in ix])
        if o.type == "Table":
            cols = []
            for c in o.columns:
                flags = (1 if c["pk"] else 0) | (2 if c["fk"] else 0) | (4 if c["unique"] else 0) | \
                        (8 if c["identity"] else 0) | (16 if c["nullable"] is False else 0)
                cols.append([c["name"], c["type"], flags, c["fk"]["table"] if c["fk"] else ""])
            det[i] = {
                "c": cols,
                "pk": o.pk_cols,
                "fk": [[f["name"] or "(unnamed)", ", ".join(f["cols"]), f["ref_table"],
                        ix.get(f["target"], -1) if f["target"] else -1] for f in o.fks],
                "ck": [[c["name"] or c["kind"], c["kind"]] for c in o.constraints],
                "ix": [[x["name"], 1 if x["unique"] else 0, ", ".join(x["cols"])] for x in o.indexes],
                "tg": [[t["name"], ix.get(t["id"], -1) if t["id"] else -1] for t in o.triggers],
                "inf": [[ix[f["from_id"]], ", ".join(f["cols"])] for f in M.incoming_fks(o.id) if f["from_id"] in ix],
            }
    return {"v": 1, "groups": M.groups, "files": files, "domains": doms, "multiDb": M.multi_db,
            "dbDeclared": M.db_declared, "objs": objs, "st": stats, "srch": srch, "uses": uses,
            "ub": used_by, "det": det, "loose": len(M.loose)}


def _data_ref(M: Model) -> dict:
    """Write the model once as data/<hash>.json; fall back to inline args if the
    app folder is read-only."""
    if "tree_ref" in M._memo:
        return M._memo["tree_ref"]
    text = json.dumps(_payload(M), separators=(",", ":"), ensure_ascii=False)
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]
    ref = {"hash": h, "dataUrl": f"data/{h}.json"}
    try:
        _DATA.mkdir(exist_ok=True)
        p = _DATA / f"{h}.json"
        if not p.exists():
            tmp = p.with_suffix(".tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(p)
        # keep the folder from growing without bound on a long-lived server
        old = sorted(_DATA.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)[30:]
        for f in old:
            f.unlink(missing_ok=True)
    except OSError:
        ref = {"hash": h, "data": json.loads(text)}
    M._memo["tree_ref"] = ref
    return ref


def index_of(M: Model) -> dict:
    if "ix" not in M._memo:
        M._memo["ix"] = {o.id: i for i, o in enumerate(M.list)}
    return M._memo["ix"]


def schema_tree(M: Model, sel, favs, recent, labels, remote=None, height=720, key="schema_tree"):
    ix = index_of(M)
    sel_i = -2 if sel == "__loose" else ix.get(sel, -1)
    args = dict(_data_ref(M), sel=sel_i, favs=sorted(ix[f] for f in favs if f in ix),
                recent=[ix[r] for r in recent if r in ix], labels=labels, remote=remote, height=height)
    return _component(key=key, default=None, **args)
