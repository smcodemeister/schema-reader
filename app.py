"""
Schema Reader — read a database from its DDL script.

Streamlit port of schema-reader.html. Upload the .sql files your database
produced (SSMS Generate Scripts, pg_dump, mysqldump, Oracle DDL) and browse
objects, relationships, lineage, change impact and an ER diagram. Nothing is
executed and no database connection is needed.

Run locally:   streamlit run app.py
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pandas as pd
import streamlit as st

from schema_engine import Model, build_erd, cluster_of, decode_sql_bytes, erd_objects, impact_dot
from tree_component import index_of, schema_tree

st.set_page_config(page_title="Schema Reader", page_icon="🗄️", layout="wide")

HERE = Path(__file__).parent
ICON = {"Table": "▤", "View": "◫", "Procedure": "▷", "Function": "ƒ", "Trigger": "⚡", "Index": "⋮",
        "Type": "τ", "Schema": "§", "Sequence": "#", "Synonym": "≡", "Database": "◈"}
TABS = ["Definition", "Columns", "Relationships", "Keys & indexes", "Used by", "Lineage", "Impact"]
WORKSPACES = ["Explorer", "Diagram", "Dashboard", "Files"]
MODES = {"db-schema-type": "Database › Schema › Type", "schema-type": "Schema › Type", "type": "Type",
         "domain": "Domain › Type", "file": "File › Type", "flat": "Flat list"}
BAND_COLOR = {"Low": "green", "Moderate": "blue", "High": "orange", "Critical": "red"}

st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; padding-bottom: 2rem;}
  [data-testid="stMetricValue"] {font-family: "IBM Plex Mono", ui-monospace, monospace;}
  .crumbs {color:#87a0b4; font-size:.85rem; margin:-.4rem 0 .4rem 0;}
  .crumbs b {color:#d7e4ee; font-weight:600;}
  .pill {display:inline-block; padding:1px 8px; border-radius:9px; font-size:.75rem;
         border:1px solid #4a3a63; color:#b394e0; background:#211b2c; margin-left:.4rem;}
  .meta {color:#87a0b4; font-family: ui-monospace, monospace; font-size:.8rem;}
  .risk {font-family: ui-monospace, monospace; font-size:2.6rem; font-weight:600; line-height:1;}
</style>
""", unsafe_allow_html=True)

# =========================================================================
# STATE
# =========================================================================
ss = st.session_state
for k, v in dict(sel=None, recent=[], favs=set(), domain_names={}, nav=0, up_key=0, use_sample=False,
                 model=None, model_sig=None, workspace="Explorer", detail_tab="Definition",
                 line_depth=3, impact_depth=6, tree_seq=None, tree_remote=None, pending_view=None).items():
    ss.setdefault(k, v)
if ss.pending_view:
    ss.workspace, ss.pending_view = ss.pending_view, None


def label(d):
    """Display name for a domain key (users can rename them)."""
    return ss.domain_names.get(d) or f"{d} Domain"


def goto(oid, tab=None, view=None):
    """Navigation lives in callbacks so it can set widget-backed state safely."""
    if not oid:
        return
    ss.sel = oid
    ss.recent = [oid] + [x for x in ss.recent if x != oid][:24]
    if tab:
        ss.detail_tab = tab
    if view:
        ss.workspace = view
    ss.nav += 1  # fresh keys for every selectable list, so no stale highlight


def toggle_fav(oid):
    favs = set(ss.favs)
    favs.symmetric_difference_update({oid})
    ss.favs = favs


def _pick_from_df(key, ids, tab=None, view=None):
    state = ss.get(key)
    rows = state["selection"]["rows"] if state else []
    if rows and rows[0] < len(ids):
        goto(ids[rows[0]], tab=tab, view=view)


def _pick_from_pills(key, tab=None, view=None):
    v = ss.get(key)
    if v:
        ss[key] = None
        goto(v, tab=tab, view=view)


def clear_all():
    ss.up_key += 1
    ss.use_sample = False
    ss.model = None
    ss.model_sig = None
    ss.sel = None
    ss.recent = []


def load_sample():
    ss.up_key += 1  # drop any uploads so the sample is what loads
    ss.use_sample = True


# =========================================================================
# SIDEBAR — loading
# =========================================================================
with st.sidebar:
    st.markdown("### 🗄️ Schema Reader")
    st.caption("Read-only · no engine. Scripts are parsed in this app; nothing is executed.")
    uploads = st.file_uploader("SQL scripts", type=["sql", "txt", "ddl"], accept_multiple_files=True,
                               key=f"up_{ss.up_key}",
                               help="Generate Scripts (SSMS), pg_dump, mysqldump or Oracle DDL. "
                                    "Add several files to compare them — UTF-16 files from SSMS are fine.")
    c1, c2 = st.columns(2)
    c1.button("Load sample", on_click=load_sample, width="stretch")
    c2.button("Clear", on_click=clear_all, width="stretch")

if uploads:
    sig = tuple((u.name, u.size, u.file_id) for u in uploads)
elif ss.use_sample:
    sig = "sample"
else:
    sig = None

if sig != ss.model_sig:
    if sig is None:
        ss.model = None
    else:
        with st.spinner("Parsing scripts…"):
            if sig == "sample":
                files = [{"name": "sample-retail.sql", "text": (HERE / "sample.sql").read_text(encoding="utf-8")}]
            else:
                files = [{"name": u.name, "text": decode_sql_bytes(u.getvalue())} for u in uploads]
                ss.use_sample = False
            ss.model = Model(files)
        M = ss.model
        first = next((o for o in M.list if o.type == "Table"), M.list[0] if M.list else None)
        ss.sel = first.id if first else None
        ss.recent = [x for x in ss.recent if x in M.objects]
        ss.favs = {x for x in ss.favs if x in M.objects}
        ss.nav += 1
        if first:
            ss.detail_tab = "Columns"
        if M.list:
            st.toast(f"{len(M.list)} objects read from {len(M.files)} file{'s' if len(M.files) > 1 else ''}")
    ss.model_sig = sig

M: Model | None = ss.model

# =========================================================================
# START SCREEN
# =========================================================================
if M is None:
    st.title("Read a database from its script")
    st.markdown(
        "Upload the **.sql** file your database produced with *Generate Scripts*, *Dump Schema* or "
        "*pg_dump* using the panel on the left — or press **Load sample** to try it on a small retail schema.")
    st.markdown(
        "- Reads T-SQL, PostgreSQL, MySQL and Oracle DDL\n"
        "- Handles UTF-16 files from SQL Server Management Studio\n"
        "- Several files at once: cross-file keys and references are traced\n"
        "- Explorer · ER diagram · lineage · change-impact scoring · summary dashboard")
    st.stop()

if not M.list:
    st.warning("No CREATE statements were found in those files. Is this a schema script?")
    st.stop()

if ss.sel not in M.objects and ss.sel != "__loose":
    ss.sel = None


def obj_label(oid):
    o = M.objects.get(oid)
    return f"{ICON.get(o.type, '•')} {o.qname}" if o else oid


def nav_pills(title, ids, key, tab=None, view=None, cap=150):
    ids = [i for i in ids if i in M.objects]
    if not ids:
        return
    k = f"{key}_{ss.nav}"
    st.pills(title, ids[:cap], format_func=obj_label, key=k, on_change=_pick_from_pills, args=(k, tab, view))
    if len(ids) > cap:
        st.caption(f"+{len(ids) - cap} more")


def nav_table(df, ids, key, tab=None, view=None, **kw):
    """A dataframe whose rows open the object they describe."""
    k = f"{key}_{ss.nav}"
    st.dataframe(df, key=k, on_select=lambda: _pick_from_df(k, ids, tab, view), selection_mode="single-row",
                 hide_index=True, width="stretch", **kw)


# =========================================================================
# SIDEBAR — workspace + explorer controls
# =========================================================================
with st.sidebar:
    n_tables = sum(1 for o in M.list if o.type == "Table")
    n_rel = sum(len(o.fks) for o in M.list)
    st.caption(f"**{len(M.list)}** objects · **{n_tables}** tables · **{n_rel}** relationships · "
               f"{len(M.files)} file{'s' if len(M.files) > 1 else ''}")
    st.radio("Workspace", WORKSPACES, key="workspace", horizontal=True)
    st.divider()

    st.download_button("Export data dictionary (.md)", M.data_dictionary(label), file_name="data-dictionary.md",
                       mime="text/markdown", width="stretch")


# =========================================================================
# EXPLORER
# =========================================================================
def render_tree():
    ev = schema_tree(M, ss.sel, ss.favs, ss.recent, dict(ss.domain_names), remote=ss.tree_remote, height=760)
    if not ev or ev.get("seq") == ss.tree_seq:
        return
    ss.tree_seq = ev["seq"]
    act = ev.get("action")
    i = ev.get("i")
    oid = M.list[i].id if isinstance(i, int) and 0 <= i < len(M.list) else None
    if act == "open" and oid:
        goto(oid, tab=ev.get("tab"))
    elif act == "loose":
        ss.sel = "__loose"
    elif act == "fav" and oid:
        toggle_fav(oid)          # the tree already shows it; the header button reads it below
    elif act == "diagram" and oid:
        goto(oid)
        ss.pending_view = "Diagram"
        st.rerun()
    elif act == "rename":
        names = dict(ss.domain_names)
        if (ev.get("name") or "").strip():
            names[ev["key"]] = ev["name"].strip()
        else:
            names.pop(ev.get("key"), None)
        ss.domain_names = names
    elif act == "search":
        q, deep = ev.get("q") or "", bool(ev.get("deep"))
        ix = index_of(M)
        hits = [[ix[r[0]], r[1]] for r in (M.search(q, deep) or []) if r[0] in ix]
        ss.tree_remote = {"q": q, "deep": deep, "hits": hits}
        st.rerun()               # hand the results back to the tree


def render_detail():
    if ss.sel == "__loose":
        st.markdown("#### ≡ Other statements")
        st.markdown('<div class="meta">not tied to a single object</div>', unsafe_allow_html=True)
        st.code("\n\n".join(x.text for x in M.loose), language="sql")
        return
    o = M.objects.get(ss.sel)
    if not o:
        st.info("Pick an object on the left. Its definition, columns, relationships, lineage and impact appear here.")
        return
    s = M.st(o.id)
    h1, h2 = st.columns([5, 1.3])
    with h1:
        st.markdown(f"#### {ICON.get(o.type, '•')} {o.qname}")
        crumbs = []
        if M.db_declared or M.multi_db:
            crumbs.append(o.group)
        if o.schema:
            crumbs.append(o.schema)
        crumbs.append(o.type + "s")
        st.markdown(f'<div class="crumbs">{" › ".join(crumbs)} › <b>{o.name}</b>'
                    f'<span class="pill" title="Inferred business domain">{label(M.domain_of(o))}</span></div>',
                    unsafe_allow_html=True)
        meta = [o.type] + ([o.group] if M.multi_db else []) + [o.file,
                f"{len(o.parts)} statement{'s' if len(o.parts) > 1 else ''}"] + ([f"{s['cols']} columns"] if s["cols"] else [])
        st.markdown(f'<div class="meta">{" · ".join(meta)}</div>', unsafe_allow_html=True)
    with h2:
        fav = o.id in ss.favs
        st.button("★ Favourite" if fav else "☆ Favourite", on_click=toggle_fav, args=(o.id,),
                  type="primary" if fav else "secondary", width="stretch")
        if o.type in ("Table", "View"):
            st.button("Show in diagram", on_click=goto, args=(o.id, None, "Diagram"), width="stretch")

    if ss.detail_tab not in TABS:
        ss.detail_tab = "Definition"
    st.radio("Section", TABS, key="detail_tab", horizontal=True, label_visibility="collapsed")
    tab = ss.detail_tab
    {"Definition": tab_definition, "Columns": tab_columns, "Relationships": tab_relationships,
     "Keys & indexes": tab_keys, "Used by": tab_used_by, "Lineage": tab_lineage, "Impact": tab_impact}[tab](o)


def tab_definition(o):
    st.code(o.code(), language="sql", line_numbers=True)


def tab_columns(o):
    if not o.columns:
        st.caption("This object declares no columns in the script.")
        return
    rows, targets = [], []
    for i, c in enumerate(o.columns, 1):
        keys = " ".join(k for k in [c["pk"] and "PK", c["fk"] and "FK", c["unique"] and not c["pk"] and "UQ",
                                    c["identity"] and "auto"] if k)
        ref = ""
        if c["fk"]:
            ref = c["fk"]["table"] + ("." + c["fk"]["cols"][0] if c["fk"]["cols"] else "")
            if not c["fk"].get("target_id"):
                ref += " (not loaded)"
            else:
                targets.append(c["fk"]["target_id"])
        rows.append({"#": i, "Column": c["name"], "Type": c["type"],
                     "Null": "no" if c["nullable"] is False else "yes" if c["nullable"] is True else "—",
                     "Key": keys, "Default": c["def"] or (c["extra"] if c["computed"] else ""), "References": ref})
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 height=min(700, 38 + 35 * len(rows)))
    nav_pills("Go to referenced table", list(dict.fromkeys(targets)), "colfk", tab="Columns")


def _fk_rows(fks, o, incoming):
    rows = []
    for f in fks:
        other = M.objects.get(f["from_id"] if incoming else f["target"]) if (f.get("from_id") or f.get("target")) else None
        r = {"Constraint": f["name"] or "—",
             "From": other.qname if incoming else o.qname,
             "Column(s)": ", ".join(f["cols"]),
             "To": o.qname if incoming else (other.qname if other else f"{f['ref_table']} (not in any loaded script)"),
             "Ref column(s)": ", ".join(f["ref_cols"])}
        if M.multi_file:
            there = other.file if other else ""
            r["Other end lives in"] = "—" if not there else "same file" if there == o.file else there
        r["Rules"] = " · ".join(x for x in [f["on_delete"] and "del " + f["on_delete"],
                                            f["on_update"] and "upd " + f["on_update"]] if x)
        rows.append(r)
    return pd.DataFrame(rows)


def tab_relationships(o):
    if o.type != "Table":
        st.caption("Foreign keys only exist on tables — see *Used by* and *Lineage* for this object's links.")
        return
    inc = M.incoming_fks(o.id)
    st.markdown(f"**Points at ({len(o.fks)})**")
    if o.fks:
        ids = [f["target"] for f in o.fks]
        nav_table(_fk_rows(o.fks, o, False), [i or o.id for i in ids], "relout", tab="Relationships")
    else:
        st.caption("This table holds no foreign keys.")
    st.markdown(f"**Pointed at by ({len(inc)})**")
    if inc:
        nav_table(_fk_rows(inc, o, True), [f["from_id"] for f in inc], "relin", tab="Relationships")
    else:
        st.caption("Nothing references this table.")
    if M.multi_file and len(o.files) > 1:
        st.caption(f"Defined across {len(o.files)} files: " + ", ".join(o.files))
    st.caption("Click a row to open the table at the other end.")


def tab_keys(o):
    st.markdown(f"**Constraints ({len(o.constraints)})**")
    if o.constraints:
        st.dataframe(pd.DataFrame([{"Kind": c["kind"], "Name": c["name"] or "—",
                                    "Definition": " ".join(c["text"].split())[:300]} for c in o.constraints]),
                     hide_index=True, width="stretch")
    else:
        st.caption("None declared.")
    st.markdown(f"**Indexes ({len(o.indexes)})**")
    if o.indexes:
        st.dataframe(pd.DataFrame([{"Name": x["name"], "Unique": "yes" if x["unique"] else "no",
                                    "Columns": ", ".join(x["cols"])} for x in o.indexes]),
                     hide_index=True, width="stretch")
    else:
        st.caption("None in this script.")
    if o.triggers:
        st.markdown(f"**Triggers ({len(o.triggers)})**")
        nav_pills("Open trigger", [t["id"] for t in o.triggers if t["id"]], "trg", tab="Definition")


def tab_used_by(o):
    by, uses = M.ref_by(o.id), M.refs(o.id)
    st.markdown(f"**Referenced by ({len(by)})**")
    nav_pills("Referenced by", by, "uby") if by else st.caption("Nothing found.")
    st.markdown(f"**References ({len(uses)})**")
    nav_pills("References", uses, "uses") if uses else st.caption("Nothing found.")
    st.caption("Matched on object names appearing in the text of each definition, "
               "so a name that doubles as a common word may over-report.")


def _levels(levels, key, empty):
    if not levels:
        st.caption(empty)
        return
    for i, ids in enumerate(levels):
        nav_pills(f"{i + 1} hop{'s' if i else ''}", ids, f"{key}{i}")


def tab_lineage(o):
    d = st.slider("Depth (hops)", 1, 8, key="line_depth")
    up, down = M.outgoing(o.id, d), M.incoming(o.id, d)
    st.caption(f"{up['total']} upstream · {down['total']} downstream")
    st.markdown(f"**Outgoing — what {o.name} reads ({up['total']})**")
    _levels(up["levels"], "lo", "This object does not name anything else in the loaded scripts.")
    st.markdown(f"**Incoming — what reads {o.name} ({down['total']})**")
    _levels(down["levels"], "li", "Nothing in the loaded scripts reads this object.")
    st.caption("Traversal is breadth-first and cycle-safe: an object already seen closer to the start is never "
               "revisited, so a circular reference shows once at its shortest distance.")


def tab_impact(o):
    depth = st.slider("Trace depth", 1, 10, key="impact_depth")
    imp = M.impact(o.id, depth)
    c = st.columns([1.3, 1, 1, 1, 1, 1])
    c[0].markdown(f'<div class="risk">{imp["score"]}</div>', unsafe_allow_html=True)
    c[0].markdown(f':{BAND_COLOR[imp["band"]]}[**{imp["band"]}**] risk')
    c[1].metric("Direct", imp["direct"])
    c[2].metric("Indirect", imp["indirect"])
    c[3].metric("Affected", imp["total"])
    c[4].metric("Depth", imp["depth"])
    c[5].metric("Cross-schema", imp["cross"])
    st.progress(imp["score"] / 100)
    if imp["total"]:
        st.markdown("**Impact graph**")
        st.graphviz_chart(impact_dot(M, imp), width="stretch")
    st.markdown(f"**Objects affected by a change ({imp['total']})**")
    _levels(imp["levels"], "imp", "Nothing else in the loaded scripts depends on this object — "
                                  "it can be changed in isolation.")
    if imp["by_type"]:
        st.markdown("**Affected by type**")
        st.dataframe(pd.DataFrame(sorted(imp["by_type"].items(), key=lambda x: -x[1]), columns=["Type", "Count"]),
                     hide_index=True)
    st.caption("The score weighs direct dependents most heavily, then indirect ones, how many hops the change can "
               "travel, and how many affected objects sit in another schema or database. It is a ranking aid for "
               "review, not a measurement.")


# =========================================================================
# DIAGRAM
# =========================================================================
@st.cache_data(show_spinner=False, max_entries=8)
def dot_to_svg(dot_src: str) -> bytes | None:
    exe = shutil.which("dot")
    if not exe:
        return None
    try:
        r = subprocess.run([exe, "-Tsvg", "-Gbgcolor=#101c26"], input=dot_src.encode(), capture_output=True, timeout=90)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def render_diagram():
    c = st.columns([1.1, 1.1, 1, 1, 1])
    scope = c[0].selectbox("Show", ["tables", "both", "views"],
                           format_func={"tables": "Tables only", "both": "Tables and views",
                                        "views": "Views (+ what they read)"}.get)
    cluster = c[1].selectbox("Cluster by", ["db", "schema", "domain", "none"],
                             format_func={"db": "Database" if M.db_declared else "Script group",
                                          "schema": "Schema", "domain": "Domain", "none": "No clustering"}.get)
    base = erd_objects(M, scope)
    keys_only = c[2].toggle("Key columns only", value=len(base) > 60,
                            help="Switch between all columns and keys only")
    hide_orphans = c[3].toggle("Hide unlinked", value=False)
    trace = c[4].toggle("Trace lineage", value=True,
                        help="Highlight what flows in and out of the focused object")

    c = st.columns([2, 1, 1, 2])
    ids = [o.id for o in sorted(base, key=lambda o: o.qname.lower())]
    opts = [None] + ids
    idx = opts.index(ss.sel) if ss.sel in ids else 0
    focus = c[0].selectbox("Focus on", opts, index=idx,
                           format_func=lambda i: "— nothing selected —" if i is None else obj_label(i))
    if focus and focus != ss.sel:
        ss.sel = focus
        ss.recent = [focus] + [x for x in ss.recent if x != focus][:24]
    hops = c[1].number_input("Neighbourhood (hops)", 0, 10, value=0 if len(base) <= 120 else 2,
                             help="0 draws everything; otherwise only objects within this many links of the focus")
    tdepth = c[2].number_input("Trace depth", 1, 10, value=4)
    all_keys = sorted({cluster_of(M, o, cluster, label) for o in base}) if cluster != "none" else []
    hidden = c[3].multiselect("Hide groups", all_keys) if len(all_keys) > 1 else []

    dot, info = build_erd(M, scope=scope, cluster=cluster, keys_only=keys_only, hide_orphans=hide_orphans,
                          trace=trace, sel=focus, trace_depth=int(tdepth), focus=focus, hops=int(hops),
                          hidden=set(hidden), label=label)
    parts = [f"{info['tables']} table{'s' if info['tables'] != 1 else ''}"]
    if info["views"]:
        parts.append(f"{info['views']} view{'s' if info['views'] != 1 else ''}")
    parts.append(f"{info['fks']} relationship{'s' if info['fks'] != 1 else ''}")
    if info["reads"]:
        parts.append(f"{info['reads']} view link{'s' if info['reads'] != 1 else ''}")
    if info["xdb"]:
        parts.append(f"{info['xdb']} across databases")
    if info["clusters"]:
        parts.append(f"{info['clusters']} groups · {info['crossing']} crossing")
    if info["traced"] is not None:
        parts.append(f"tracing {info['traced']} related, {info['direct']} direct")
    st.caption(" · ".join(parts))
    st.caption("Legend — crow's foot = many end of a foreign key · dashed green arrow = view reads object · "
               "brass = crosses databases · dashed = crosses a group frame · "
               "with tracing: cyan = focus, green border = upstream (it reads), red border = downstream (reads it).")

    if not info["nodes"]:
        st.info("No views found in these scripts." if scope == "views" else "Nothing to draw with these settings.")
        return
    if info["nodes"] > 250 and not st.checkbox(
            f"Draw all {info['nodes']} objects anyway (may be slow in the browser)", value=False):
        st.warning("That is a large diagram. Pick an object in **Focus on** and set **Neighbourhood** to 1–3 hops, "
                   "or hide some groups, to keep it readable.")
    else:
        st.graphviz_chart(dot, width="stretch")

    d1, d2, d3 = st.columns([1, 1, 2])
    d1.download_button("Download DOT", dot, file_name="diagram.dot", mime="text/vnd.graphviz",
                       width="stretch")
    svg = dot_to_svg(dot)
    if svg:
        d2.download_button("Download SVG", svg, file_name="diagram.svg", mime="image/svg+xml",
                           width="stretch")
    if focus:
        d3.button(f"Open {M.objects[focus].name} in Explorer", on_click=goto, args=(focus, "Columns", "Explorer"))


# =========================================================================
# DASHBOARD
# =========================================================================
def _rank_table(title, rows, unit, key):
    st.markdown(f"**{title}**")
    if not rows:
        st.caption("Nothing to rank yet.")
        return
    df = pd.DataFrame([{"": ICON.get(r[0].type, "•"), "Object": r[0].qname, "Value": r[1], "Note": r[2]}
                       for r in rows])
    nav_table(df, [r[0].id for r in rows], key, tab="Columns", view="Explorer",
              column_config={"": st.column_config.TextColumn(width=30),
                             "Value": st.column_config.ProgressColumn(format="%d", min_value=0,
                                                                      max_value=max(1, max(r[1] for r in rows)))})
    st.caption(unit)


def render_dashboard():
    tables = [o for o in M.list if o.type == "Table"]
    cnt = {t: sum(1 for o in M.list if o.type == t) for t in ("Table", "View", "Procedure", "Function", "Trigger")}
    kpis = [(len(M.groups), "Databases" if M.db_declared else "Script groups"),
            (len({(o.group, o.schema) for o in M.list}), "Schemas"), (len(M.domain_list), "Domains"),
            (cnt["Table"], "Tables"), (cnt["View"], "Views"), (cnt["Procedure"], "Procedures"),
            (cnt["Function"], "Functions"), (cnt["Trigger"], "Triggers"),
            (sum(len(o.indexes) for o in M.list), "Indexes"), (sum(len(o.constraints) for o in M.list), "Constraints"),
            (sum(len(o.fks) for o in M.list), "Relationships"), (sum(len(o.columns) for o in M.list), "Columns")]
    for row in range(0, len(kpis), 6):
        cols = st.columns(6)
        for col, (v, l) in zip(cols, kpis[row:row + 6]):
            col.metric(l, f"{v:,}")
    st.divider()

    st_ = M.st
    top_ref = sorted(((o, st_(o.id)["in_fk"], f"{st_(o.id)['in_fk']} foreign keys point here") for o in tables
                      if st_(o.id)["in_fk"]), key=lambda r: -r[1])[:10]
    connected = sorted(((o, st_(o.id)["fks"] + st_(o.id)["in_fk"], f"{st_(o.id)['fks']} out · {st_(o.id)['in_fk']} in")
                        for o in tables if st_(o.id)["fks"] + st_(o.id)["in_fk"]), key=lambda r: -r[1])[:10]
    hotspots = sorted(((o, st_(o.id)["refs"] + st_(o.id)["ref_by"],
                        f"{st_(o.id)['refs']} referenced · {st_(o.id)['ref_by']} dependents") for o in M.list
                       if st_(o.id)["refs"] + st_(o.id)["ref_by"]), key=lambda r: -r[1])[:10]
    risky = [(i["o"], i["score"], f"{i['band']} · {i['total']} affected, {i['depth']} deep") for i in M.risk_ranking(4)]

    a, b = st.columns(2, gap="large")
    with a:
        _rank_table("Top referenced tables", top_ref, "Counted from resolved foreign keys pointing at each table.", "d1")
        _rank_table("Dependency hotspots", hotspots, "Every object, counted across name references and keys.", "d3")
    with b:
        _rank_table("Most connected tables", connected, "Foreign keys in both directions.", "d2")
        _rank_table("Highest change risk", risky, "Score from direct and indirect dependents, depth and reach.", "d4")

    a, b = st.columns(2, gap="large")
    with a:
        st.markdown("**Largest schemas**")
        by_schema = {}
        for o in M.list:
            k = (o.group + " · " if M.multi_db else "") + (o.schema or "(no schema)")
            by_schema[k] = by_schema.get(k, 0) + 1
        sr = sorted(by_schema.items(), key=lambda x: -x[1])[:10]
        st.dataframe(pd.DataFrame(sr, columns=["Schema", "Objects"]), hide_index=True, width="stretch",
                     column_config={"Objects": st.column_config.ProgressColumn(format="%d", min_value=0,
                                                                               max_value=max(x[1] for x in sr))})

        st.markdown("**Cross-schema dependencies**")
        pairs = {}
        for o in M.list:
            for oid in M.refs(o.id):
                t = M.objects.get(oid)
                if t and not (t.schema == o.schema and t.group == o.group):
                    k = f"{o.group}.{o.schema or '—'} → {t.group}.{t.schema or '—'}"
                    pairs[k] = pairs.get(k, 0) + 1
        if pairs:
            st.dataframe(pd.DataFrame(sorted(pairs.items(), key=lambda x: -x[1])[:12],
                                      columns=["Direction", "References"]), hide_index=True, width="stretch")
        else:
            st.caption("Every reference stays inside its own schema.")
    with b:
        st.markdown("**Business domains**")
        dr = [(label(d), M.domain_counts[d]) for d in M.domain_list]
        st.dataframe(pd.DataFrame(dr, columns=["Domain", "Objects"]), hide_index=True, width="stretch",
                     column_config={"Objects": st.column_config.ProgressColumn(format="%d", min_value=0,
                                                                               max_value=max(x[1] for x in dr))})
        st.caption("Inferred from naming, key clustering and schema ownership.")
        with st.expander("Rename a domain"):
            labels = {label(k): k for k in M.domain_list}
            d = labels[st.selectbox("Domain", list(labels))]
            new = st.text_input("New name", value=label(d), key=f"dn_{d}")
            r1, r2 = st.columns(2)
            if r1.button("Save name", width="stretch"):
                names = dict(ss.domain_names)
                if new.strip():
                    names[d] = new.strip()
                else:
                    names.pop(d, None)
                ss.domain_names = names
                st.rerun()
            if r2.button("Reset", width="stretch"):
                ss.domain_names = {k: v for k, v in ss.domain_names.items() if k != d}
                st.rerun()

        fk, ref, unresolved = M.cross_links()
        st.markdown("**Cross-file dependencies**")
        st.dataframe(pd.DataFrame([("Foreign keys spanning files", len(fk)),
                                   ("Code reaching into another file", len(ref)),
                                   ("References to objects never loaded", len(unresolved))],
                                  columns=["Kind", "Count"]), hide_index=True, width="stretch")

    orphans = [o for o in tables if not M.st(o.id)["fks"] and not M.st(o.id)["in_fk"]]
    st.markdown(f"**Tables with no relationships ({len(orphans)})**")
    if orphans:
        nav_pills("Orphan tables", [o.id for o in orphans], "orph", tab="Columns", view="Explorer", cap=80)
    else:
        st.caption("Every table is joined to at least one other.")


# =========================================================================
# FILES
# =========================================================================
def render_files():
    fk, ref, unresolved = M.cross_links()
    per = {f["name"]: {"obj": 0, "tbl": 0, "touch": 0, "dbs": []} for f in M.files}
    for o in M.list:
        r = per.get(o.file)
        if r:
            r["obj"] += 1
            r["tbl"] += o.type == "Table"
            if o.group not in r["dbs"]:
                r["dbs"].append(o.group)
        for fn in o.files:
            if fn != o.file and fn in per:
                per[fn]["touch"] += 1
    st.markdown(f"**Scripts loaded ({len(M.files)})**")
    st.dataframe(pd.DataFrame([{"File": f["name"], "Databases": ", ".join(per[f["name"]]["dbs"]) or "—",
                                "Objects defined": per[f["name"]]["obj"], "Tables": per[f["name"]]["tbl"],
                                "Statements added to objects elsewhere": per[f["name"]]["touch"],
                                "Size (KB)": round(len(f["text"]) / 1024)} for f in M.files]),
                 hide_index=True, width="stretch")

    if M.multi_file:
        names = [f["name"] for f in M.files]
        ix = {n: i for i, n in enumerate(names)}
        mat = [[0] * len(names) for _ in names]

        def bump(a, b):
            if a in ix and b in ix and a != b:
                mat[ix[a]][ix[b]] += 1
        for l in fk:
            bump(l["decl"], l["frm"].file)
            bump(l["decl"], l["to"].file)
        for l in ref:
            bump(l["frm"].file, l["to"].file)
        short = [n.rsplit(".", 1)[0] for n in names]
        df = pd.DataFrame([["·" if i == j else (v or "·") for j, v in enumerate(row)] for i, row in enumerate(mat)],
                          index=names, columns=short).astype(str)
        st.markdown("**Which script depends on which** (row reaches into column)")
        st.dataframe(df, width="stretch")
        st.caption("A number means objects in the row's script reach into the column's script. "
                   "Load those two together or the row's script will not deploy on its own.")

    st.markdown(f"**Foreign keys that cross files ({len(fk)})**")
    if fk:
        nav_table(pd.DataFrame([{"Constraint": l["fk"]["name"] or "—", "Declared in": l["decl"],
                                 "From": f"{l['frm'].qname} ({', '.join(l['fk']['cols'])})", "Lives in": l["frm"].file,
                                 "To": l["to"].qname, "Lives in ": l["to"].file} for l in fk]),
                  [l["frm"].id for l in fk], "ffk", tab="Relationships", view="Explorer")
        st.caption("A key is self-contained only when all three file columns name the same script.")
    else:
        st.caption("Every foreign key sits in the same script as both tables it joins.")

    st.markdown(f"**Views, procedures and functions reaching into another file ({len(ref)})**")
    if ref:
        shown = ref[:300]
        nav_table(pd.DataFrame([{"": ICON.get(l["frm"].type, "•"), "Object": l["frm"].qname, "In file": l["frm"].file,
                                 "Uses": l["to"].qname, "In file ": l["to"].file} for l in shown]),
                  [l["frm"].id for l in shown], "fref", tab="Lineage", view="Explorer")
    else:
        st.caption("No code in one script names an object defined in another.")

    st.markdown(f"**References to objects that are not in any loaded script ({len(unresolved)})**")
    if unresolved:
        nav_table(pd.DataFrame([{"Constraint": u["fk"]["name"] or "—", "Declared in": u["decl"], "From": u["frm"].qname,
                                 "Points at": (u["fk"]["ref_schema"] + "." if u["fk"]["ref_schema"] else "")
                                 + u["fk"]["ref_table"]} for u in unresolved]),
                  [u["frm"].id for u in unresolved], "funres", tab="Relationships", view="Explorer")
        st.caption("Usually a script you have not uploaded yet — add it to the uploader alongside the others.")
    else:
        st.caption("Every foreign key found a table to point at.")


# =========================================================================
# ROUTER
# =========================================================================
if ss.workspace == "Explorer":
    left, right = st.columns([4, 7], gap="medium")
    with left:
        render_tree()
    with right:
        render_detail()
elif ss.workspace == "Diagram":
    render_diagram()
elif ss.workspace == "Dashboard":
    render_dashboard()
else:
    render_files()
