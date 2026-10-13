"""
schema_engine.py — the parser and analysis engines behind Schema Reader.

A straight port of the JavaScript in the original schema-reader.html. Nothing
here imports Streamlit, so the engine can be tested or reused on its own.

  1. lexical mask        comments blanked, string/identifier contents hidden
  2. statement split     GO batches, or top-level semicolons (+ MySQL DELIMITER)
  3. identifiers         [bracketed], "quoted", `backticked`, multi-part names
  4. classifier          CREATE/ALTER of every object kind
  5. table bodies        columns, types, defaults, PK/FK/UNIQUE/CHECK
  6. whole-script parse  ALTER TABLE ADD CONSTRAINT, indexes, triggers
  7. linking             name references, FK resolution, incoming-FK index
  8. engines             dependency graph, lineage, impact score, domains,
                         statistics, search, cross-file analysis, ERD (DOT)
"""
from __future__ import annotations

import colorsys
import re
from collections import defaultdict

# =========================================================================
# 1. LEXICAL MASK
# Same-length copy of the script where comments become spaces and the
# *inside* of strings / quoted identifiers becomes 'x'. Regexes run against
# the mask; text is sliced from the original by index.
# =========================================================================
_SPECIAL = re.compile(r"--|/\*|['\"`\[$]")
_BLOCK = re.compile(r"/\*|\*/")
_DOLLAR = re.compile(r"\$[A-Za-z_]\w*\$|\$\$")
_MYSQL_DELIM = re.compile(r"(?:^|\n)[ \t]*DELIMITER[ \t]+\S", re.I)
_DOLLAR_BODY = re.compile(r"(\$[A-Za-z_]\w*\$|\$\$)([\s\S]*?)\1")
_LINE_COMMENT = re.compile(r"--[^\n]*")


def build_mask(sql: str) -> str:
    n = len(sql)
    m = list(sql)
    i = 0
    find = sql.find
    # a MySQL script that switches DELIMITER often uses $$ as the delimiter,
    # which must not be mistaken for PostgreSQL dollar quoting
    dollar_ok = not _MYSQL_DELIM.search(sql)
    while i < n:
        hit = _SPECIAL.search(sql, i)
        if not hit:
            break
        i = hit.start()
        c = sql[i]
        if c == "-":  # line comment
            j = find("\n", i)
            if j < 0:
                j = n
            m[i:j] = " " * (j - i)
            i = j
            continue
        if c == "/":  # block comment, T-SQL allows nesting
            d, j = 1, i + 2
            while j < n and d > 0:
                b = _BLOCK.search(sql, j)
                if not b:
                    j = n
                    break
                d += 1 if b.group() == "/*" else -1
                j = b.end()
            m[i:j] = " " * (j - i)
            i = j
            continue
        if c in "'\"`[":  # literal / quoted identifier / [bracketed]
            q = "]" if c == "[" else c
            j = i + 1
            while True:
                k = find(q, j)
                if k < 0:
                    j = n
                    break
                if k + 1 < n and sql[k + 1] == q:
                    j = k + 2
                    continue
                j = k + 1
                break
            end = max(i + 1, j - 1)
            m[i + 1:end] = "x" * (end - i - 1)
            if j - 1 > i:
                m[j - 1] = q
            i = j
            continue
        if c == "$" and dollar_ok:  # $$ … $$ / $tag$ … $tag$
            mt = _DOLLAR.match(sql, i)
            if mt:
                tag = mt.group()
                j = find(tag, i + len(tag))
                j = n if j < 0 else j + len(tag)
                m[i:j] = "x" * (j - i)
                i = j
                continue
        i += 1
    return "".join(m)


# =========================================================================
# 2. STATEMENT SPLIT
# =========================================================================
class Stmt:
    __slots__ = ("text", "mask", "file", "offset", "db")

    def __init__(self, text, mask, file, offset):
        self.text, self.mask, self.file, self.offset, self.db = text, mask, file, offset, ""


_GO = re.compile(r"(^|\n)([ \t]*GO[ \t]*\d*[ \t]*)(\r?\n|$)", re.I)
_DELIM = re.compile(r"DELIMITER[ \t]+(\S+)", re.I)
_NONSPACE = re.compile(r"\S")


def split_statements(sql: str, mask: str, file: str) -> list[Stmt]:
    out: list[Stmt] = []

    def push(a, b):
        # trim both with the SAME indices so offsets stay aligned
        s, e = a, b
        while s < e and sql[s].isspace():
            s += 1
        while e > s and sql[e - 1].isspace():
            e -= 1
        if s < e and _NONSPACE.search(mask, s, e):
            out.append(Stmt(sql[s:e], mask[s:e], file, s))

    if _GO.search(mask):
        last = 0
        for mt in _GO.finditer(mask):
            push(last, mt.start() + len(mt.group(1)))
            last = mt.end()
        push(last, len(sql))
        return out

    depth, last, delim, i, L = 0, 0, ";", 0, len(mask)
    while i < L:
        ch = mask[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0:
            if delim == ";" and ch == ";":
                push(last, i)
                last = i + 1
            elif delim != ";" and mask.startswith(delim, i):
                push(last, i)
                i += len(delim) - 1
                last = i + 1
        if i == 0 or mask[i - 1] == "\n":
            d = _DELIM.match(mask, i)
            if d:
                push(last, i)
                delim = d.group(1)
                nl = mask.find("\n", i)
                i = L if nl < 0 else nl
                last = i + 1
        i += 1
    push(last, len(sql))
    return out


# =========================================================================
# 3. IDENTIFIERS
# =========================================================================
_ID = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_$#@")


def read_name(sql: str, mask: str, pos: int):
    """Read a possibly-qualified name starting at pos -> (parts, end)."""
    parts, i, L = [], pos, len(mask)

    def skip(k):
        while k < L and mask[k].isspace():
            k += 1
        return k

    while True:
        i = skip(i)
        if i >= L:
            break
        c = mask[i]
        if c == "[":
            e = mask.find("]", i)
            if e < 0:
                break
            parts.append(sql[i + 1:e])
            i = e + 1
        elif c in "\"`":
            e = mask.find(c, i + 1)
            if e < 0:
                break
            parts.append(sql[i + 1:e])
            i = e + 1
        elif c in _ID:
            s = i
            while i < L and mask[i] in _ID:
                i += 1
            parts.append(sql[s:i])
        else:
            break
        save = i
        i = skip(i)
        if i < L and mask[i] == ".":
            i += 1
            continue
        i = save
        break
    return parts, i


def qualify(parts):
    n = len(parts)
    if not n:
        return {"db": "", "schema": "", "name": "(unnamed)"}
    return {"db": parts[n - 3] if n > 2 else "", "schema": parts[n - 2] if n > 1 else "", "name": parts[n - 1]}


def key_of(t, db, s, n):
    """Two databases may each hold a dbo.Customer; they are not the same table."""
    return "|".join(x.lower() for x in (t, db or "", s or "", n or ""))


def file_base(n):
    return re.sub(r"\.[^.]+$", "", str(n))


# =========================================================================
# 4. STATEMENT CLASSIFIER
# =========================================================================
NOISE = (r"(?:UNIQUE|CLUSTERED|NONCLUSTERED|COLUMNSTORE|FULLTEXT|SPATIAL|MATERIALIZED|TEMPORARY|TEMP|"
         r"UNLOGGED|GLOBAL|LOCAL|EXTERNAL|RECURSIVE|OR\s+REPLACE|OR\s+ALTER|IF\s+NOT\s+EXISTS|"
         r"DEFINER\s*=\s*[^\s]+|ALGORITHM\s*=\s*\w+|SQL\s+SECURITY\s+\w+)")
KINDS = (r"TABLE|VIEW|PROCEDURE|PROC|FUNCTION|TRIGGER|INDEX|TYPE|SCHEMA|SEQUENCE|SYNONYM|DATABASE|"
         r"AGGREGATE|DOMAIN|RULE|EXTENSION|PARTITION\s+FUNCTION|PARTITION\s+SCHEME|"
         r"XML\s+SCHEMA\s+COLLECTION|MESSAGE\s+TYPE|FULLTEXT\s+CATALOG|LOGIN|USER|ROLE")
RE_CREATE = re.compile(r"\s*(CREATE|ALTER)\s+(?:" + NOISE + r"\s+)*(" + KINDS + r")\b", re.I)
RE_IFNE = re.compile(r"\s*IF\s+NOT\s+EXISTS\b", re.I)
RE_ALTER_TABLE = re.compile(r"\s*ALTER\s+TABLE\s+(?:ONLY\s+)?", re.I)

TYPE_LABEL = {"TABLE": "Table", "VIEW": "View", "PROCEDURE": "Procedure", "PROC": "Procedure",
              "FUNCTION": "Function", "TRIGGER": "Trigger", "INDEX": "Index", "TYPE": "Type",
              "SCHEMA": "Schema", "SEQUENCE": "Sequence", "SYNONYM": "Synonym", "DATABASE": "Database",
              "AGGREGATE": "Aggregate", "DOMAIN": "Domain", "RULE": "Rule", "EXTENSION": "Extension",
              "LOGIN": "Login", "USER": "User", "ROLE": "Role"}


def type_label(k):
    k = re.sub(r"\s+", " ", k).upper()
    return TYPE_LABEL.get(k) or (k[:1] + k[1:].lower())


def match_paren(mask, p):
    d = 0
    for i in range(p, len(mask)):
        c = mask[i]
        if c == "(":
            d += 1
        elif c == ")":
            d -= 1
            if not d:
                return i
    return -1


def split_commas(mask, a, b):
    out, d, s = [], 0, a
    for i in range(a, b):
        c = mask[i]
        if c == "(":
            d += 1
        elif c == ")":
            d -= 1
        elif c == "," and d == 0:
            out.append((s, i))
            s = i + 1
    out.append((s, b))
    return out


def col_list(sql, mask, open_paren):
    if open_paren < 0:
        return []
    close = match_paren(mask, open_paren)
    if close < 0:
        return []
    out = []
    for a, b in split_commas(mask, open_paren + 1, close):
        parts, _ = read_name(sql, mask, a)
        v = parts[-1] if parts else sql[a:b].strip()
        if v:
            out.append(v)
    return out


# =========================================================================
# 5. TABLE BODY -> columns + constraints
# =========================================================================
class Obj:
    def __init__(self, type_, db, schema, name, file):
        self.id = key_of(type_, db, schema, name)
        self.type, self.db, self.schema, self.name, self.file = type_, db, schema, name, file
        self.parts: list[Stmt] = []
        self.columns: list[dict] = []
        self.constraints: list[dict] = []
        self.fks: list[dict] = []
        self.indexes: list[dict] = []
        self.triggers: list[dict] = []
        self.pk_cols: list[str] = []
        self.uses: list[str] = []
        self.used_by: list[str] = []
        self.on = None
        self.files: list[str] = []
        self.group = ""

    @property
    def qname(self):
        return (self.schema + "." if self.schema else "") + self.name

    def code(self):
        return "\nGO\n\n".join(p.text.rstrip() for p in self.parts)

    def __repr__(self):
        return f"<{self.type} {self.qname}>"


_TYPE_CONT = re.compile(r"\s+(?:PRECISION|VARYING|UNSIGNED|SIGNED|ZEROFILL|BYTE|WITH\s+TIME\s+ZONE|"
                        r"WITHOUT\s+TIME\s+ZONE|WITH\s+LOCAL\s+TIME\s+ZONE|CHARACTER\s+SET\s+\w+|"
                        r"CHARACTER|NATIONAL|RAW)\b", re.I)
_TYPE_HEAD = re.compile(r'\s*(?:\[[^\]]*\]|"[^"]*"|[A-Za-z_][\w$]*)(?:\s*\.\s*(?:\[[^\]]*\]|"[^"]*"|[A-Za-z_][\w$]*))*')
_TYPE_PAREN = re.compile(r"\s*\((?:[^()]|\([^()]*\))*\)")
_TYPE_ARR = re.compile(r"\s*(?:\[\s*\d*\s*\])+")
_CONSTRAINT_START = re.compile(r"\s*(CONSTRAINT\b|PRIMARY\s+KEY\b|FOREIGN\s+KEY\b|UNIQUE\b|CHECK\b|KEY\b|INDEX\b|EXCLUDE\b|PERIOD\s+FOR\b)", re.I)


def parse_table_body(sql, mask, open_paren, obj):
    close = match_paren(mask, open_paren)
    if close < 0:
        return
    for a, b in split_commas(mask, open_paren + 1, close):
        seg = mask[a:b]
        if not seg.strip():
            continue
        if _CONSTRAINT_START.match(seg):
            parse_constraint(sql, mask, a, b, obj)
        else:
            parse_column(sql, mask, a, b, obj)


def _action_of(txt, which):
    m = re.search(r"ON\s+" + which + r"\s+(CASCADE|SET\s+NULL|SET\s+DEFAULT|NO\s+ACTION|RESTRICT)", txt, re.I)
    return re.sub(r"\s+", " ", m.group(1)).upper() if m else ""


def parse_column(sql, mask, a, b, obj):
    parts, end = read_name(sql, mask, a)
    if not parts:
        return
    col = {"name": parts[-1], "type": "", "nullable": None, "identity": False, "computed": False,
           "pk": False, "fk": None, "unique": False, "def": "", "extra": ""}
    i = end
    rest, rest_raw = mask[i:b], sql[i:b]
    if re.match(r"\s*AS\b", rest, re.I):
        col["computed"] = True
        col["type"] = "computed"
        col["extra"] = re.sub(r"^\s*AS\s*", "", rest_raw, flags=re.I).strip()
    else:
        tm = _TYPE_HEAD.match(rest)
        if tm and tm.group().strip():
            p = tm.end()
            while (c := _TYPE_CONT.match(rest, p)):
                p = c.end()
            pm = _TYPE_PAREN.match(rest, p)
            if pm:
                p = pm.end()
            while (c := _TYPE_CONT.match(rest, p)):
                p = c.end()
            am = _TYPE_ARR.match(rest, p)
            if am:
                p = am.end()
            t = re.sub(r"\s+", " ", rest_raw[:p].strip())
            t = re.sub(r"\s*\(\s*", "(", t, count=1)
            col["type"] = re.sub(r"\s*\)", ")", t, count=1)
            i += p
        tail, tail_raw = mask[i:b], sql[i:b]
        if re.search(r"\bNOT\s+NULL\b", tail, re.I):
            col["nullable"] = False
        elif re.search(r"\bNULL\b", tail, re.I) and not re.search(r"\bDEFAULT\s+NULL\b", tail, re.I):
            col["nullable"] = True
        if (re.search(r"\b(IDENTITY|AUTO_INCREMENT|GENERATED\s+(ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY|SERIAL)\b", tail, re.I)
                or re.search(r"SERIAL", col["type"], re.I)):
            col["identity"] = True
        if re.search(r"\bPRIMARY\s+KEY\b", tail, re.I):
            col["pk"] = True
        if re.search(r"\bUNIQUE\b", tail, re.I):
            col["unique"] = True
        dm = re.search(r"\bDEFAULT\s+", tail, re.I)
        if dm:
            s = j = dm.end()
            d = 0
            while j < len(tail):
                c = tail[j]
                if c == "(":
                    d += 1
                elif c == ")":
                    if not d:
                        break
                    d -= 1
                elif not d and (c.isspace() or c == ",") and j > s:
                    break
                j += 1
            col["def"] = tail_raw[s:j].strip()
        rm = re.search(r"\bREFERENCES\s+", tail, re.I)
        if rm:
            rparts, rend = read_name(sql, mask, i + rm.end())
            t = qualify(rparts)
            to_cols = []
            p = mask.find("(", rend)
            if p >= 0 and not mask[rend:p].strip():
                to_cols = col_list(sql, mask, p)
            col["fk"] = {"db": t["db"], "schema": t["schema"], "table": t["name"], "cols": to_cols, "target_id": None}
            obj.fks.append({"name": "", "cols": [col["name"]], "ref_db": t["db"], "ref_schema": t["schema"],
                            "ref_table": t["name"], "ref_cols": to_cols,
                            "on_delete": _action_of(tail, "DELETE"), "on_update": _action_of(tail, "UPDATE")})
    obj.columns.append(col)


def _strip_order(c):
    return re.sub(r"\s+(ASC|DESC)$", "", str(c), flags=re.I).strip()


def _mark_cols(obj, cols, flag):
    s = {c.lower() for c in cols}
    for c in obj.columns:
        if c["name"].lower() in s:
            c[flag] = True


def parse_constraint(sql, mask, a, b, obj):
    i, name = a, ""
    cm = re.match(r"\s*CONSTRAINT\s+", mask[a:b], re.I)
    if cm:
        parts, i = read_name(sql, mask, a + cm.end())
        name = parts[-1] if parts else ""
    seg, seg_raw = mask[i:b], sql[i:b]
    fkm = re.match(r"\s*FOREIGN\s+KEY\s*", seg, re.I)
    if fkm:
        p = mask.find("(", i + fkm.end() - 1)
        cols = col_list(sql, mask, p) if 0 <= p < b else []
        rm = re.search(r"\bREFERENCES\s+", seg, re.I)
        if rm:
            rparts, rend = read_name(sql, mask, i + rm.end())
            t = qualify(rparts)
            to_cols = []
            p2 = mask.find("(", rend)
            if 0 <= p2 < b and not mask[rend:p2].strip():
                to_cols = col_list(sql, mask, p2)
            obj.fks.append({"name": name, "cols": cols, "ref_db": t["db"], "ref_schema": t["schema"],
                            "ref_table": t["name"], "ref_cols": to_cols,
                            "on_delete": _action_of(seg, "DELETE"), "on_update": _action_of(seg, "UPDATE")})
        obj.constraints.append({"kind": "FOREIGN KEY", "name": name, "cols": cols, "text": seg_raw.strip()})
        return
    pkm = re.match(r"\s*(PRIMARY\s+KEY|UNIQUE|KEY|INDEX)\b", seg, re.I)
    if pkm:
        kind = re.sub(r"\s+", " ", pkm.group(1).upper())
        p = mask.find("(", i)
        cols = [_strip_order(c) for c in col_list(sql, mask, p)] if 0 <= p < b else []
        if kind == "PRIMARY KEY":
            obj.pk_cols = cols
            _mark_cols(obj, cols, "pk")
        elif kind == "UNIQUE":
            _mark_cols(obj, cols, "unique")
        obj.constraints.append({"kind": kind, "name": name, "cols": cols, "text": seg_raw.strip()})
        return
    # SSMS style: ADD CONSTRAINT DF_x DEFAULT ((0)) FOR ColumnName
    dm = re.search(r"\bDEFAULT\s+([\s\S]+?)\s+FOR\s+", seg, re.I)
    if dm:
        tparts, _ = read_name(sql, mask, i + dm.end())
        target = (tparts[-1] if tparts else "").lower()
        for c in obj.columns:
            if c["name"].lower() == target and not c["def"]:
                c["def"] = seg_raw[dm.start(1):dm.end(1)].strip()
                break
        obj.constraints.append({"kind": "DEFAULT", "name": name, "cols": [], "text": seg_raw.strip()})
        return
    obj.constraints.append({"kind": "CHECK" if re.search(r"CHECK", seg, re.I) else "OTHER",
                            "name": name, "cols": [], "text": seg_raw.strip()})


# =========================================================================
# 6. PARSE A WHOLE SCRIPT SET
# =========================================================================
_ADD_CONS = re.compile(r"\bADD\s+(?:CONSTRAINT\b|PRIMARY\s+KEY\b|FOREIGN\s+KEY\b|UNIQUE\b|CHECK\b|DEFAULT\b)", re.I)
_ADD_COL = re.compile(r"\bADD\s+(?:COLUMN\s+)?(?=(?:\[|\"|`|[A-Za-z_])[\w$\]\"`]*\s+(?:\[|\"|`|[A-Za-z_]))", re.I)


def parse_all(files):
    objects: dict[str, Obj] = {}
    tables_by_name: dict[tuple, Obj] = {}
    loose: list[Stmt] = []
    pending: list[dict] = []

    def get(type_, db, schema, name, file, create):
        k = key_of(type_, db, schema, name)
        o = objects.get(k)
        if o is None and create:
            o = Obj(type_, db, schema, name, file)
            objects[k] = o
            if type_ == "Table":
                tables_by_name.setdefault((db, name.lower()), o)
        return o

    def find_table(db, schema, name):
        # a table may be altered before it is created; fall back to name within the same database
        return objects.get(key_of("Table", db, schema, name)) or tables_by_name.get((db, (name or "").lower()))

    for f in files:
        mask = build_mask(f["text"])
        cur_db = ""
        for st in split_statements(f["text"], mask, f["name"]):
            um = re.match(r"\s*USE\s+", st.mask, re.I)
            if um:
                p, _ = read_name(st.text, st.mask, um.end())
                cur_db = p[-1] if p else cur_db
            dbm = re.match(r"\s*CREATE\s+DATABASE\s+", st.mask, re.I)
            if dbm:
                p, _ = read_name(st.text, st.mask, dbm.end())
                cur_db = p[-1] if p else cur_db
            st.db = cur_db
            # ALTER TABLE first: it also matches the CREATE pattern
            if RE_ALTER_TABLE.match(st.mask):
                pending.append({"type": "AlterTable", "st": st})
                continue
            m = RE_CREATE.match(st.mask)
            if m:
                type_ = type_label(m.group(2))
                after = m.end()
                ifne = RE_IFNE.match(st.mask, after)
                if ifne:
                    after = ifne.end()
                parts, rend = read_name(st.text, st.mask, after)
                q = qualify(parts)
                db = q["name"] if type_ == "Database" else (q["db"] or st.db or "")
                if type_ in ("Index", "Trigger"):
                    pending.append({"type": type_, "st": st, "name": q, "db": db, "after": rend})
                    continue
                o = get(type_, db, q["schema"], q["name"], st.file, True)
                o.parts.append(st)
                if not o.file:
                    o.file = st.file
                if type_ == "Table":
                    p = st.mask.find("(", rend)
                    if p >= 0 and not st.mask[rend:p].strip():
                        parse_table_body(st.text, st.mask, p, o)
                    for fk in o.fks:
                        fk.setdefault("declared_in", st.file)
                continue
            loose.append(st)

    # second pass: things that hang off a table
    for p in pending:
        st = p["st"]
        if p["type"] == "AlterTable":
            head = RE_ALTER_TABLE.match(st.mask)
            parts, rend = read_name(st.text, st.mask, head.end())
            q = qualify(parts)
            db = q["db"] or st.db or ""
            o = find_table(db, q["schema"], q["name"]) or get("Table", db, q["schema"], q["name"], st.file, True)
            o.parts.append(st)
            body = st.mask[rend:]
            add = _ADD_CONS.search(body)
            if add:
                skip = re.match(r"ADD\s+", body[add.start():], re.I).end()
                parse_constraint(st.text, st.mask, rend + add.start() + skip, len(st.mask), o)
                for fk in o.fks:
                    fk.setdefault("declared_in", st.file)
            else:
                addc = _ADD_COL.search(body)
                if addc:
                    parse_column(st.text, st.mask, rend + addc.end(), len(st.mask), o)
        else:
            after = p["after"]
            on = re.search(r"\bON\s+", st.mask[after:], re.I)
            owner = None
            if on:
                op, _ = read_name(st.text, st.mask, after + on.end())
                q2 = qualify(op)
                owner = find_table(q2["db"] or p["db"], q2["schema"], q2["name"])
            pp = st.mask.find("(", after)
            cols = col_list(st.text, st.mask, pp) if (pp >= 0 and on and pp > on.start() + after) else []
            rec = {"type": p["type"], "name": p["name"]["name"], "schema": p["name"]["schema"], "db": p["db"],
                   "cols": [_strip_order(c) for c in cols], "id": None,
                   "unique": bool(re.search(r"\bCREATE\s+(?:\w+\s+)*UNIQUE\b", st.mask, re.I))}
            if owner:
                owner.parts.append(st)
                if p["type"] == "Trigger":
                    t = get("Trigger", p["db"], p["name"]["schema"], p["name"]["name"], st.file, True)
                    t.parts.append(st)
                    t.on = owner.id
                    rec["id"] = t.id
                (owner.indexes if p["type"] == "Index" else owner.triggers).append(rec)
            else:
                o = get(p["type"], p["db"], p["name"]["schema"], p["name"]["name"], st.file, True)
                o.parts.append(st)
    return objects, loose


# =========================================================================
# 7/8. MODEL — parse, link, then every engine
# =========================================================================
ORDER = ["Table", "View", "Procedure", "Function", "Trigger", "Type", "Sequence", "Synonym", "Index", "Schema"]


def type_rank(t):
    return ORDER.index(t) if t in ORDER else 99


GEN_A = re.compile(r"^(sys|MSpeer_|MSpub_|sp_MS|dt_|__|aspnet_|trace_xe|filestream_)", re.I)
GEN_B = re.compile(r"(_bak|_backup|_old|_copy\d*|_tmp|_temp|_history|_hist)$", re.I)


def is_generated(o):
    return bool(GEN_A.search(o.name) or GEN_B.search(o.name) or o.name.lower() == "sysdiagrams")


_WORD = re.compile(r"[A-Za-z_][\w$]*")
_DELIM_ID = re.compile(r"\[([^\]]+)\]|\"([^\"]+)\"|`([^`]+)`")
_BLANK = re.compile(r"\[x*\]|\"x*\"|`x*`")
_THREE = re.compile(r"(?:\[([^\]]+)\]|\"([^\"]+)\"|([A-Za-z_][\w$]*))\s*\.\s*(?:\[[^\]]*\]|\"[^\"]*\"|[A-Za-z_][\w$]*)?"
                    r"\s*\.\s*(?:\[([^\]]+)\]|\"([^\"]+)\"|([A-Za-z_][\w$]*))")


class Model:
    def __init__(self, files):
        """files: list of {"name": str, "text": str}"""
        self.files = files
        self.objects, self.loose = parse_all(files)
        self.list = sorted(self.objects.values(),
                           key=lambda o: (type_rank(o.type), (o.schema + o.name).lower()))
        for o in self.list:
            o.files = list(dict.fromkeys(p.file for p in o.parts))
            o.group = o.db or file_base(o.file)
        self.file_ix = {f["name"]: i for i, f in enumerate(files)}
        self.groups = sorted({o.group for o in self.list})
        self.group_ix = {g: i for i, g in enumerate(self.groups)}
        self.db_declared = any(o.db for o in self.list)
        self._link_names()
        self._resolve_fks()
        self._index_incoming()
        self._build_graph()
        self._domains()
        self._stats()
        self._search_docs()
        self._memo = {}

    # ---- helpers ---------------------------------------------------------
    @property
    def multi_db(self):
        return len(self.groups) > 1

    @property
    def multi_file(self):
        return len(self.files) > 1

    def get(self, oid):
        return self.objects.get(oid)

    # ---- who mentions whom ----------------------------------------------
    def _link_names(self):
        by_name = defaultdict(list)
        for o in self.list:
            if o.type != "Index":
                by_name[o.db.lower() + "|" + o.name.lower()].append(o)
        for o in self.list:
            seen = set()
            home = o.db.lower() + "|"

            def hit(cands):
                if not cands:
                    return
                for t in cands:
                    if t is not o and t.id not in seen:
                        seen.add(t.id)
                        o.uses.append(t.id)
                        t.used_by.append(o.id)

            for p in o.parts:
                src = _BLANK.sub(lambda g: " " * len(g.group()), p.mask)
                for w in dict.fromkeys(_WORD.findall(src)):
                    hit(by_name.get(home + w.lower()))
                # PostgreSQL function bodies are dollar-quoted, so the mask hid them
                if "$" in p.text:
                    for body in _DOLLAR_BODY.finditer(p.text):
                        for w in dict.fromkeys(_WORD.findall(_LINE_COMMENT.sub(" ", body.group(2)))):
                            hit(by_name.get(home + w.lower()))
                for m in _DELIM_ID.finditer(p.text):
                    hit(by_name.get(home + (m.group(1) or m.group(2) or m.group(3)).lower()))
                for m in _THREE.finditer(p.text):
                    d = (m.group(1) or m.group(2) or m.group(3) or "").lower()
                    nm = (m.group(4) or m.group(5) or m.group(6) or "").lower()
                    if d and nm and d != o.db.lower():
                        hit(by_name.get(d + "|" + nm))

    def _resolve_fks(self):
        tables = [o for o in self.list if o.type == "Table"]
        by_qual = {t.id: t for t in tables}
        by_bare = {}
        for t in tables:
            by_bare.setdefault(t.db.lower() + "|" + t.name.lower(), t)
        for t in tables:
            for fk in t.fks:
                db = fk["ref_db"] or t.db
                target = (by_qual.get(key_of("Table", db, fk["ref_schema"], fk["ref_table"]))
                          or by_bare.get(db.lower() + "|" + (fk["ref_table"] or "").lower()))
                fk["target"] = target.id if target else None
                fk["owner"] = t.id
                fk["ref_db"] = db
                fk.setdefault("declared_in", t.file)
                for ix, cn in enumerate(fk["cols"]):
                    c = next((c for c in t.columns if c["name"].lower() == str(cn).lower()), None)
                    if c and (not c["fk"] or not c["fk"].get("target_id")):
                        c["fk"] = {"db": db, "schema": fk["ref_schema"], "table": fk["ref_table"],
                                   "cols": [fk["ref_cols"][ix]] if ix < len(fk["ref_cols"]) else fk["ref_cols"],
                                   "target_id": fk["target"]}

    def _index_incoming(self):
        self.in_fk = defaultdict(list)
        for t in self.list:
            if t.type != "Table":
                continue
            for f in t.fks:
                if f["target"]:
                    self.in_fk[f["target"]].append(dict(f, from_id=t.id))

    def incoming_fks(self, oid):
        return self.in_fk.get(oid, [])

    # ---- dependency engine ----------------------------------------------
    # out[a] -> b : "a reads or depends on b";  inn[b] -> a : "a would notice if b changed"
    def _build_graph(self):
        self.out = defaultdict(dict)
        self.inn = defaultdict(dict)

        def edge(a, b):
            if not a or not b or a == b or a not in self.objects or b not in self.objects:
                return
            self.out[a][b] = True
            self.inn[b][a] = True

        for o in self.list:
            for oid in o.uses:
                edge(o.id, oid)
            if o.type == "Table":
                for f in o.fks:
                    if f["target"]:
                        edge(o.id, f["target"])
            if o.type == "Trigger" and o.on:
                edge(o.id, o.on)

    def refs(self, oid):
        return list(self.out.get(oid, {}))

    def ref_by(self, oid):
        return list(self.inn.get(oid, {}))

    def walk(self, oid, direction, max_depth=64):
        """Breadth-first, cycle-safe, depth-limited; objects grouped by hop count."""
        m = self.out if direction == "out" else self.inn
        seen, levels = {}, []
        frontier, guard = [oid], {oid}
        for d in range(1, (max_depth or 64) + 1):
            nxt = []
            for cur in frontier:
                for x in m.get(cur, {}):
                    if x in guard:
                        continue
                    guard.add(x)
                    seen[x] = d
                    nxt.append(x)
            if not nxt:
                break
            levels.append(nxt)
            frontier = nxt
            if len(seen) > 8000:
                break
        return {"levels": levels, "seen": seen, "total": len(seen), "depth": len(levels)}

    def impact(self, oid, max_depth=6):
        o = self.objects.get(oid)
        if not o:
            return None
        w = self.walk(oid, "in", max_depth)
        direct = len(self.inn.get(oid, {}))
        indirect = max(0, w["total"] - direct)
        by_type, cross = defaultdict(int), 0
        for k in w["seen"]:
            t = self.objects.get(k)
            if not t:
                continue
            by_type[t.type] += 1
            if t.schema != o.schema or t.group != o.group:
                cross += 1
        raw = (direct * 6 + indirect * 1.6 + w["depth"] * 5 + cross * 2.2
               + (6 if o.type == "Table" else 0) + (2 if o.pk_cols else 0))
        score = max(0, min(100, round(raw)))
        band = ("Low" if score < 20 else "Moderate" if score < 45 else "High" if score < 70 else "Critical")
        return dict(o=o, direct=direct, indirect=indirect, total=w["total"], depth=w["depth"],
                    levels=w["levels"], seen=w["seen"], by_type=dict(by_type), cross=cross,
                    score=score, band=band)

    # ---- lineage --------------------------------------------------------
    def outgoing(self, oid, d):  # this reads …
        return self.walk(oid, "out", d)

    def incoming(self, oid, d):  # … reads this
        return self.walk(oid, "in", d)

    def lineage_map(self, oid, d):
        m = {}
        for k, v in self.outgoing(oid, d)["seen"].items():
            m[k] = {"dir": "up", "depth": v}
        for k, v in self.incoming(oid, d)["seen"].items():
            cur = m.get(k)
            if not cur or v < cur["depth"]:
                m[k] = {"dir": "both" if cur else "down", "depth": min(v, cur["depth"]) if cur else v}
        return m

    # ---- domains --------------------------------------------------------
    def _domains(self):
        asg = {}
        tables = [o for o in self.list if o.type == "Table"]
        for t in tables:  # 1 · vocabulary
            for rx, d in DOMAIN_RULES:
                if rx.search(t.name):
                    asg[t.id] = d
                    break
        pre = defaultdict(list)  # 2 · shared prefixes
        for t in tables:
            if t.id in asg:
                continue
            p = _name_prefix(t.name)
            if p:
                pre[p.lower()].append(t)
        for arr in pre.values():
            if len(arr) >= 2:
                nm = _title(_name_prefix(arr[0].name) or arr[0].name)
                for t in arr:
                    asg[t.id] = nm

        def vote(oid):
            votes = defaultdict(int)
            for x in self.out.get(oid, {}):
                if x in asg:
                    votes[asg[x]] += 2
            for x in self.inn.get(oid, {}):
                if x in asg:
                    votes[asg[x]] += 1
            best, bv = None, 0
            for d, v in votes.items():
                if v > bv:
                    best, bv = d, v
            return best

        for _ in range(4):  # 3 · let the FK graph pull stragglers in
            moved = 0
            for t in tables:
                if t.id in asg:
                    continue
                b = vote(t.id)
                if b:
                    asg[t.id] = b
                    moved += 1
            if not moved:
                break
        for t in tables:  # 4 · schema ownership
            if t.id not in asg:
                asg[t.id] = _title(t.schema) if t.schema else "General"
        for o in self.list:  # everything else follows the tables it touches
            if o.type != "Table":
                asg[o.id] = vote(o.id) or (_title(o.schema) if o.schema else "General")
        self.domain = asg
        counts = defaultdict(int)
        for d in asg.values():
            counts[d] += 1
        self.domain_counts = dict(counts)
        self.domain_list = sorted(counts, key=lambda d: (-counts[d], d))

    def domain_of(self, o):
        return self.domain.get(o.id if isinstance(o, Obj) else o, "General")

    # ---- statistics -----------------------------------------------------
    def _stats(self):
        self.stat = {}
        for o in self.list:
            self.stat[o.id] = dict(cols=len(o.columns), fks=len(o.fks), in_fk=len(self.in_fk.get(o.id, [])),
                                   idx=len(o.indexes), trg=len(o.triggers), cons=len(o.constraints),
                                   refs=len(self.out.get(o.id, {})), ref_by=len(self.inn.get(o.id, {})))

    def st(self, oid):
        return self.stat.get(oid) or dict(cols=0, fks=0, in_fk=0, idx=0, trg=0, cons=0, refs=0, ref_by=0)

    # ---- search ---------------------------------------------------------
    def _search_docs(self):
        j = "\u0001"
        self.docs = []
        for o in self.list:
            self.docs.append(dict(
                id=o.id, o=o, lname=o.name.lower(), qname=o.qname.lower(), schema=(o.schema or "").lower(),
                db=(o.group or "").lower(), otype=o.type.lower(), domain=self.domain_of(o).lower(),
                cols=j.join(c["name"] for c in o.columns).lower(),
                types=j.join(dict.fromkeys(c["type"] for c in o.columns if c["type"])).lower(),
                cons=j.join(c["name"] for c in o.constraints if c["name"]).lower(),
                idx=j.join(i["name"] for i in o.indexes if i["name"]).lower(),
                refs=j.join((f["ref_schema"] + "." if f["ref_schema"] else "") + f["ref_table"] for f in o.fks).lower(),
                code=None))
        self._code_built = False

    def _build_code(self):
        if not self._code_built:
            for d in self.docs:
                d["code"] = d["o"].code().lower()
            self._code_built = True

    def search(self, q, deep=False):
        """Every term has to land somewhere, so two words narrow rather than widen.
        Returns [(id, score, [where...])] best first, or None for an empty query."""
        terms = parse_query(q)
        if not terms:
            return None
        if deep or any(t["field"] == "code" for t in terms):
            self._build_code()
        out = []
        for d in self.docs:
            total, where, ok = 0, [], True
            for t in terms:
                r = self._score_term(d, t, deep)
                if not r:
                    ok = False
                    break
                total += r[0]
                if r[1] not in where:
                    where.append(r[1])
            if ok:
                out.append((d["id"], total, where))
        out.sort(key=lambda r: (-r[1], type_rank(self.objects[r[0]].type), self.objects[r[0]].name))
        return out

    def _score_term(self, d, t, deep):
        q = t["text"]
        f = t["field"]
        if f:
            if f == "code":
                return (110, "code") if d["code"] and q in d["code"] else None
            if f == "otype":
                return (240, "type") if d["otype"].startswith(q) else None
            v = d.get(f)
            return (320, FIELD_LABEL.get(f, f)) if v and q in v else None
        if d["lname"] == q:
            return (1000, "name")
        if d["lname"].startswith(q):
            return (760, "name")
        if q in d["lname"]:
            return (560, "name")
        if q in d["qname"]:
            return (480, "name")
        for field, score, label in (("schema", 320, "schema"), ("db", 300, "database"), ("cols", 260, "column")):
            if q in d[field]:
                return (score, label)
        if d["otype"].startswith(q):
            return (200, "type")
        for field, score, label in (("types", 170, "data type"), ("cons", 160, "constraint"), ("idx", 150, "index"),
                                    ("refs", 140, "reference"), ("domain", 130, "domain")):
            if q in d[field]:
                return (score, label)
        if deep and d["code"] and q in d["code"]:
            return (70, "code")
        fz = fuzzy_score(d["lname"], q)
        if fz > 0.42:
            return (round(fz * 230), "name")
        return None

    # ---- cross-file analysis -------------------------------------------
    def cross_links(self):
        if "cross" in self._memo:
            return self._memo["cross"]
        fk, ref, unresolved = [], [], []
        for t in self.list:
            if t.type != "Table":
                continue
            for f in t.fks:
                decl = f.get("declared_in") or t.file
                if not f["target"]:
                    unresolved.append(dict(frm=t, fk=f, decl=decl))
                    continue
                tgt = self.objects.get(f["target"])
                if not tgt:
                    continue
                # only self-contained when all three sit in the same script
                if tgt.file != t.file or decl != t.file or decl != tgt.file:
                    fk.append(dict(frm=t, to=tgt, fk=f, decl=decl))
        for o in self.list:
            for oid in dict.fromkeys(o.uses):
                t = self.objects.get(oid)
                if t and t.file != o.file and not (o.type == "Table" and t.type == "Table"):
                    ref.append(dict(frm=o, to=t))
        self._memo["cross"] = (fk, ref, unresolved)
        return self._memo["cross"]

    def risk_ranking(self, depth=4, limit=10):
        key = ("risk", depth, limit)
        if key not in self._memo:
            imps = [self.impact(o.id, depth) for o in self.list]
            imps = sorted((i for i in imps if i and i["score"] > 0), key=lambda i: -i["score"])[:limit]
            self._memo[key] = imps
        return self._memo[key]

    # ---- export ---------------------------------------------------------
    def data_dictionary(self, label=lambda d: d + " Domain"):
        md = "# Data dictionary\n\nGenerated from " + ", ".join(f["name"] for f in self.files) + "\n"
        for t in (o for o in self.list if o.type == "Table"):
            md += "\n## " + t.qname + "\n\n"
            md += "Domain: " + label(self.domain_of(t)) + "\n\n"
            md += "| Column | Type | Null | Key | Default |\n|---|---|---|---|---|\n"
            for c in t.columns:
                k = ", ".join(x for x in [c["pk"] and "PK", c["fk"] and "FK → " + c["fk"]["table"],
                                          c["unique"] and not c["pk"] and "UQ", c["identity"] and "auto"] if x)
                nul = "no" if c["nullable"] is False else "yes" if c["nullable"] is True else ""
                md += f"| {c['name']} | {c['type']} | {nul} | {k} | {c['def']} |\n"
            if t.fks:
                md += "\nForeign keys:\n"
                for f in t.fks:
                    md += (f"- {f['name'] or '(unnamed)'}: ({', '.join(f['cols'])}) → "
                           f"{f['ref_table']}({', '.join(f['ref_cols'])})\n")
        return md


# ---- domain vocabulary ----------------------------------------------------
DOMAIN_RULES = [(re.compile(p, re.I), d) for p, d in [
    (r"audit|history|changelog|eventlog|journal|archive", "Audit"),
    (r"user|role|permission|privilege|login|security|auth|credential|token|principal|claim", "Security"),
    (r"customer|client|contact|party|person|member|subscriber|patient|supplier|vendor", "Customer"),
    (r"order|cart|basket|checkout|invoice|payment|billing|shipment|shipping|delivery|refund", "Order"),
    (r"product|item|sku|catalog|inventory|stock|price|pricing|offer", "Product"),
    (r"employee|staff|payroll|department|position|timesheet|absence", "Workforce"),
    (r"config|setting|parameter|option|feature|toggle|preference", "Configuration"),
    (r"queue|batch|staging|^stg_|^tmp_|^temp_|^etl|import|export", "Processing"),
    (r"report|summary|aggregate|^fact_|^dim_|kpi|metric|statistic|snapshot", "Analytics"),
    (r"lookup|reference|^ref[_A-Z]|code$|codes$|type$|types$|status|country|region|currency|language|category", "Reference"),
]]


def _title(s):
    s = str(s)
    return s[:1].upper() + s[1:]


def _name_prefix(name):
    s = re.sub(r"^(tbl|tb|t)_", "", str(name), flags=re.I)
    m = re.match(r"([A-Za-z][a-z]{2,})(?=[A-Z]|_|\d|$)", s)
    if m:
        return m.group(1)
    m = re.match(r"([A-Za-z]{3,}?)_", s)
    return m.group(1) if m else None


# ---- search helpers -------------------------------------------------------
FIELD_ALIAS = {"col": "cols", "column": "cols", "columns": "cols", "type": "types", "datatype": "types",
               "schema": "schema", "db": "db", "database": "db", "index": "idx", "idx": "idx", "key": "cons",
               "constraint": "cons", "ref": "refs", "reference": "refs", "code": "code", "is": "otype",
               "t": "otype", "kind": "otype", "domain": "domain"}
FIELD_LABEL = {"cols": "column", "types": "data type", "cons": "constraint", "idx": "index", "refs": "reference",
               "schema": "schema", "db": "database", "code": "code", "otype": "type", "domain": "domain"}
_QTERM = re.compile(r"(?:(\w+):)?(?:\"([^\"]+)\"|(\S+))")


def parse_query(q):
    terms = []
    for m in _QTERM.finditer(q or ""):
        raw = (m.group(2) or m.group(3) or "").lower()
        if not raw:
            continue
        f = FIELD_ALIAS.get(m.group(1).lower()) if m.group(1) else None
        terms.append({"field": f, "text": raw, "plain": not m.group(1)})
    return terms


def fuzzy_score(s, pat):
    if len(pat) < 2 or len(pat) > len(s):
        return 0
    i = j = run = best = 0
    first = -1
    while i < len(s) and j < len(pat):
        if s[i] == pat[j]:
            if first < 0:
                first = i
            j += 1
            run += 1
            best = max(best, run)
        else:
            run = 0
        i += 1
    if j < len(pat):
        return 0
    return min(1, (best / len(pat)) * 0.6 + (len(pat) / len(s)) * 0.3 + (0.1 if first == 0 else 0))


# =========================================================================
# FILE DECODING — SSMS often writes UTF-16, sometimes with no BOM
# =========================================================================
def decode_sql_bytes(b: bytes) -> str:
    if b[:2] == b"\xff\xfe":
        enc, b = "utf-16-le", b[2:]
    elif b[:2] == b"\xfe\xff":
        enc, b = "utf-16-be", b[2:]
    else:
        n = min(len(b), 1024)
        zeros = sum(1 for i in range(1, n, 2) if b[i] == 0)
        enc = "utf-16-le" if zeros > n / 5 else "utf-8"
    for e in (enc, "utf-8", "cp1252"):
        try:
            text = b.decode(e)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = b.decode("utf-8", errors="replace")
    return text[1:] if text.startswith("﻿") else text


# =========================================================================
# COLOURS — one hue per group, spread far enough apart to stay tellable
# =========================================================================
HUES = [205, 272, 142, 36, 330, 186, 96, 14, 255, 116, 300, 58]


def _hsl(h, s, l):
    r, g, b = colorsys.hls_to_rgb((h % 360) / 360, l / 100, s / 100)
    return "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255))


def group_color(i):
    h = (HUES[i % len(HUES)] + (i // len(HUES)) * 17) % 360
    return dict(hd=_hsl(h, 36, 27), body=_hsl(h, 24, 14), stroke=_hsl(h, 34, 44),
                wash=_hsl(h, 30, 18), ink=_hsl(h, 45, 72))


# =========================================================================
# DIAGRAM — Graphviz DOT
# =========================================================================
C = dict(text="#c6d8e6", title="#e6f2fa", dim="#6f8ba1", faint="#3d5366", brass="#d9a441", cyan="#5ac8e8",
         sage="#8fbf7f", rose="#e0798a", rel="#4b7f99", reads="#78ab93", body="#16242f", hd="#213a4c",
         line="#2d4a61", fade_body="#111c25", fade_line="#1c2c3a")


def _h(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def _q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def erd_objects(M: Model, scope):
    """tables — the classic ERD; both — plus views; views — every view plus what it reads."""
    if scope == "views":
        keep = {}
        for v in M.list:
            if v.type == "View":
                keep[v.id] = v
        for v in list(keep.values()):
            for oid in M.refs(v.id):
                t = M.objects.get(oid)
                if t and t.type in ("Table", "View"):
                    keep[oid] = t
        return list(keep.values())
    types = ("Table", "View") if scope == "both" else ("Table",)
    return [o for o in M.list if o.type in types]


def cluster_of(M: Model, o, cluster, label=lambda d: d + " Domain"):
    if cluster == "schema":
        return (o.group + " · " if M.multi_db else "") + (o.schema or "(no schema)")
    if cluster == "domain":
        return label(M.domain_of(o))
    if cluster == "none":
        return "*"
    return o.group


def build_erd(M: Model, scope="tables", cluster="db", keys_only=False, hide_orphans=False,
              trace=True, sel=None, trace_depth=4, focus=None, hops=0, hidden=(),
              label=lambda d: d + " Domain"):
    """Returns (dot_source, info). info carries counts for the status line."""
    objs = erd_objects(M, scope)
    ck = {o.id: cluster_of(M, o, cluster, label) for o in objs}
    all_keys = sorted(set(ck.values()))
    objs = [o for o in objs if ck[o.id] not in hidden]
    present = {o.id for o in objs}

    edges, seen_read = [], set()
    for t in objs:
        if t.type == "Table":
            for f in t.fks:
                if f["target"]:
                    edges.append(dict(a=t.id, b=f["target"], kind="fk", cols=f["cols"], ref_cols=f["ref_cols"]))
        if t.type == "View":
            for oid in M.refs(t.id):
                tgt = M.objects.get(oid)
                if oid == t.id or oid not in present or not tgt or tgt.type not in ("Table", "View"):
                    continue
                if (t.id, oid) in seen_read:
                    continue
                seen_read.add((t.id, oid))
                edges.append(dict(a=t.id, b=oid, kind="reads"))
    edges = [e for e in edges if e["a"] in present and e["b"] in present]

    # neighbourhood of one object, walking diagram links in either direction
    if focus and hops and focus in present:
        adj = defaultdict(set)
        for e in edges:
            adj[e["a"]].add(e["b"])
            adj[e["b"]].add(e["a"])
        keep, frontier = {focus}, {focus}
        for _ in range(hops):
            frontier = {n for x in frontier for n in adj[x]} - keep
            keep |= frontier
        objs = [o for o in objs if o.id in keep]
    if hide_orphans:
        linked = {e["a"] for e in edges} | {e["b"] for e in edges}
        objs = [o for o in objs if o.id in linked]
    keep = {o.id for o in objs}
    edges = [e for e in edges if e["a"] in keep and e["b"] in keep]

    keys_in_use = sorted({ck[o.id] for o in objs})
    grouped = cluster != "none" and len(keys_in_use) > 1
    color_ix = {k: i for i, k in enumerate(all_keys)}
    if cluster == "db":
        color_ix = {k: M.group_ix.get(k, i) for i, k in enumerate(all_keys)}

    pick = sel if (trace and sel in keep) else None
    lin = M.lineage_map(sel, trace_depth) if pick else None
    in_trace = (lambda x: x == pick or (lin is not None and x in lin))

    nid = {o.id: "n%d" % i for i, o in enumerate(objs)}
    lines = ['digraph ERD {',
             '  graph [rankdir=LR, bgcolor="transparent", pad=0.3, nodesep=0.35, ranksep=0.9, '
             'fontname="Helvetica", fontcolor="%s", compound=true];' % C["text"],
             '  node [shape=plain, fontname="Helvetica", fontsize=10];',
             '  edge [fontname="Helvetica", fontsize=9, arrowsize=0.8];']

    ports = {}

    def node_dot(o):
        is_view = o.type == "View"
        faded = lin is not None and o.id != pick and o.id not in lin
        g = group_color(color_ix.get(ck[o.id], 0)) if grouped else dict(hd=C["hd"], body=C["body"], stroke=C["line"])
        border, bw = (C["fade_line"] if faded else g["stroke"]), 1
        if lin is not None and not faded:
            if o.id == pick:
                border, bw = C["cyan"], 3
            else:
                d = lin[o.id]["dir"]
                border, bw = (C["rose"] if d == "down" else C["sage"]), 2
        ink = C["faint"] if faded else C["text"]
        body = C["fade_body"] if faded else g["body"]
        hd = C["fade_body"] if faded else g["hd"]
        title = _h((o.schema + "." if o.schema and o.schema.lower() != "dbo" else "") + o.name)
        tag = ('<FONT COLOR="%s" POINT-SIZE="8"><B>  VIEW</B></FONT>' % (C["faint"] if faded else C["sage"])) if is_view else ""
        rows = ['<TR><TD COLSPAN="2" BGCOLOR="%s" ALIGN="LEFT" CELLPADDING="5"><FONT COLOR="%s" POINT-SIZE="11"><B>%s</B></FONT>%s</TD></TR>'
                % (hd, C["faint"] if faded else C["title"], title, tag)]
        ports[o.id] = {}
        if is_view:
            reads = [x for x in (M.objects.get(i) for i in M.refs(o.id)) if x and x.type in ("Table", "View")]
            cross = sum(1 for x in reads if x.group != o.group)
            note = ("reads %d object%s" % (len(reads), "" if len(reads) == 1 else "s")
                    + (" · %d in another database" % cross if cross else "")) if reads else "no resolved sources"
            rows.append('<TR><TD COLSPAN="2" ALIGN="LEFT"><FONT COLOR="%s">%s</FONT></TD></TR>' % (C["dim"] if not faded else C["faint"], _h(note)))
        else:
            if keys_only:
                shown = [c for c in o.columns if c["pk"] or c["fk"] or c["unique"]][:14] or o.columns[:5]
            else:
                shown = o.columns[:60]
            for i, c in enumerate(shown):
                ports[o.id][c["name"].lower()] = "c%d" % i
                mark = "PK" if c["pk"] else "FK" if c["fk"] else "UQ" if c["unique"] else ""
                mc = C["brass"] if c["pk"] else C["cyan"]
                mark_html = ('<FONT COLOR="%s"><B>%s</B></FONT> ' % (C["faint"] if faded else mc, mark)) if mark else ""
                typ = re.sub(r"character varying", "varchar", c["type"] or "", flags=re.I)[:22]
                rows.append('<TR><TD PORT="c%d" ALIGN="LEFT">%s<FONT COLOR="%s">%s</FONT></TD>'
                            '<TD PORT="t%d" ALIGN="RIGHT"><FONT COLOR="%s">%s</FONT></TD></TR>'
                            % (i, mark_html, ink, _h(c["name"]), i, C["faint"] if faded else C["dim"], _h(typ)))
            more = len(o.columns) - len(shown)
            if not o.columns:
                rows.append('<TR><TD COLSPAN="2" ALIGN="LEFT"><FONT COLOR="%s">no columns in script</FONT></TD></TR>' % C["dim"])
            elif more > 0:
                rows.append('<TR><TD COLSPAN="2" ALIGN="LEFT"><FONT COLOR="%s">+%d %s</FONT></TD></TR>'
                            % (C["dim"], more, "other column" + ("s" if more > 1 else "") if keys_only else "more"))
        tip = "%s %s%s" % (o.type, o.qname, (" — " + o.group) if M.multi_db else "")
        if lin is not None and o.id in lin:
            dd = lin[o.id]["depth"]
            tip += "\n%d hop%s from the selection" % (dd, "" if dd == 1 else "s")
        label_html = ('<<TABLE BORDER="%d" CELLBORDER="0" CELLSPACING="0" CELLPADDING="3" COLOR="%s" BGCOLOR="%s">%s</TABLE>>'
                      % (bw, border, body, "".join(rows)))
        return '  %s [label=%s, tooltip=%s];' % (nid[o.id], label_html, _q(tip))

    node_lines = {o.id: node_dot(o) for o in objs}
    if grouped:
        by_k = defaultdict(list)
        for o in objs:
            by_k[ck[o.id]].append(o)
        for ci, k in enumerate(keys_in_use):
            g = group_color(color_ix.get(k, ci))
            n_t = sum(1 for o in by_k[k] if o.type == "Table")
            n_v = len(by_k[k]) - n_t
            lab = "%s   %d table%s%s" % (k, n_t, "" if n_t == 1 else "s", (", %d view%s" % (n_v, "" if n_v == 1 else "s")) if n_v else "")
            lines.append('  subgraph cluster_%d {' % ci)
            lines.append('    label=%s; labeljust=l; fontsize=14; fontcolor="%s"; style="rounded,filled"; '
                         'fillcolor="%s"; color="%s"; penwidth=1.2; margin=16;' % (_q(lab), g["ink"], g["wash"], g["stroke"]))
            lines.extend("  " + node_lines[o.id] for o in by_k[k])
            lines.append('  }')
    else:
        lines.extend(node_lines[o.id] for o in objs)

    crossing = xdb_n = 0
    for e in edges:
        a, b = M.objects[e["a"]], M.objects[e["b"]]
        reads = e["kind"] == "reads"
        xdb = a.group != b.group
        if grouped and ck[a.id] != ck[b.id]:
            crossing += 1
        if xdb:
            xdb_n += 1
        touches = pick and (e["a"] == pick or e["b"] == pick)
        on = lin is not None and in_trace(e["a"]) and in_trace(e["b"])
        color = C["brass"] if xdb else (C["reads"] if reads else C["rel"])
        width = 1.3
        if touches:
            color, width = (C["brass"] if xdb else C["cyan"]), 2.8
        elif lin is not None and not on:
            color, width = C["fade_line"], 1.0
        elif on:
            color, width = (C["brass"] if xdb else C["cyan"]), 2.0
        style = "dashed" if (reads or (grouped and ck[a.id] != ck[b.id] and not touches)) else "solid"
        if reads:
            tip = "%s reads %s%s" % (a.qname, b.qname, ("  (%s → %s)" % (a.group, b.group)) if xdb else "")
            lines.append('  %s -> %s [color="%s", penwidth=%.1f, style=%s, arrowhead=normal, tooltip=%s];'
                         % (nid[a.id], nid[b.id], color, width, style, _q(tip)))
        else:
            tp = ports[a.id].get((e["cols"][0] if e["cols"] else "").lower())
            hp = ports[b.id].get((e["ref_cols"][0] if e["ref_cols"] else "").lower()) or \
                 next((ports[b.id].get(c.lower()) for c in b.pk_cols if ports[b.id].get(c.lower())), None)
            tail = nid[a.id] + (":t" + tp[1:] + ":e" if tp else "")
            head = nid[b.id] + (":" + hp + ":w" if hp else "")
            tip = "%s(%s) → %s(%s)" % (a.name, ", ".join(e["cols"]), b.name, ", ".join(e["ref_cols"]))
            if a.id == b.id:
                tail, head = nid[a.id], nid[b.id]
            lines.append('  %s -> %s [dir=both, arrowtail=crow, arrowhead=tee, color="%s", penwidth=%.1f, style=%s, tooltip=%s];'
                         % (tail, head, color, width, style, _q(tip)))
    lines.append("}")
    nv = sum(1 for o in objs if o.type == "View")
    info = dict(tables=len(objs) - nv, views=nv, fks=sum(1 for e in edges if e["kind"] == "fk"),
                reads=sum(1 for e in edges if e["kind"] == "reads"), xdb=xdb_n, crossing=crossing,
                clusters=len(keys_in_use) if grouped else 0, all_clusters=all_keys,
                traced=len(lin) if lin is not None else None,
                direct=sum(1 for e in edges if pick and (e["a"] == pick or e["b"] == pick)),
                nodes=len(objs))
    return "\n".join(lines), info


def impact_dot(M: Model, imp, per_level=14):
    """Selected object on the left, each hop of dependents in its own column."""
    depth_col = ["#5ac8e8", "#8fbf7f", "#d9a441", "#e0798a", "#b394e0", "#7d95a8"]
    levels = [lvl[:per_level] for lvl in imp["levels"]]
    shown = {imp["o"].id: -1}
    for i, lvl in enumerate(levels):
        for x in lvl:
            shown[x] = i + 1
    out = ['digraph Impact {',
           '  graph [rankdir=LR, bgcolor="transparent", nodesep=0.18, ranksep=0.8, fontname="Helvetica"];',
           '  node [shape=box, style="rounded,filled", fillcolor="%s", fontname="Helvetica", fontsize=10, '
           'fontcolor="%s", height=0.3, margin="0.12,0.04"];' % (C["body"], C["text"]),
           '  edge [color="#31556d", arrowsize=0.6];']
    ids = {k: "i%d" % n for n, k in enumerate(shown)}
    for k, d in shown.items():
        t = M.objects.get(k)
        if not t:
            continue
        name = t.name if len(t.name) <= 22 else t.name[:21] + "…"
        c = C["cyan"] if d < 0 else depth_col[min(d - 1, len(depth_col) - 1)]
        tip = "%s · %s · %s" % (t.qname, t.type, "selected" if d < 0 else "%d hop%s away" % (d, "" if d == 1 else "s"))
        out.append('  %s [label=%s, color="%s", penwidth=%s, tooltip=%s];' % (ids[k], _q(name), c, 2.2 if d < 0 else 1.2, _q(tip)))
    for i, lvl in enumerate(imp["levels"]):
        if len(lvl) > per_level:
            out.append('  more%d [label="+%d more", shape=plaintext, style="", fontcolor="%s"];' % (i, len(lvl) - per_level, C["dim"]))
    for k in shown:
        if k == imp["o"].id:
            continue
        for src in M.refs(k):
            if src in shown and src != k:
                out.append("  %s -> %s;" % (ids[src], ids[k]))
    out.append("}")
    return "\n".join(out)
