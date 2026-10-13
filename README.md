# Schema Reader (Streamlit)

Read a database from its DDL script — no database connection, nothing executed.
Upload the `.sql` files produced by SSMS *Generate Scripts*, `pg_dump`, `mysqldump`
or Oracle DDL and explore objects, relationships, lineage, change impact and an ER diagram.

## Files

| File | Purpose |
|---|---|
| `app.py` | The Streamlit app (entry point) |
| `schema_engine.py` | Parser and analysis engines (no Streamlit dependency) |
| `tree_component.py` | The object tree as a Streamlit custom component |
| `tree_frontend/index.html` | The tree's front end — the original page's windowed tree, no build step |
| `sample.sql` | Sample retail schema behind the **Load sample** button |
| `requirements.txt` | Python packages |
| `packages.txt` | System package (Graphviz) — enables the **Download SVG** button on Streamlit Cloud |
| `.streamlit/config.toml` | Dark theme matching the original page, 200 MB upload limit |

## Deploy on Streamlit Community Cloud

1. Create a new GitHub repository and upload **all** the files above, keeping
   `.streamlit/config.toml` inside a folder called `.streamlit`.
2. Go to <https://share.streamlit.io>, choose **Create app → Deploy a public app from GitHub**.
3. Pick the repository and branch, set **Main file path** to `app.py`, and deploy.

## Run locally

```bash
pip install -r requirements.txt
streamlit run app.py
```

## What's where

- **Explorer** — the original object tree: Database › Schema › Type › Object (or Schema, Type, Domain,
  File, Flat, Dependency and Lineage views), tables opening into Columns, Keys, Indexes, Triggers and
  Used By folders, Favourites and Recently viewed pinned at the top, a type filter, a right-click menu and
  arrow-key navigation (`/` searches, `F` favourites, `I` impact, `L` lineage, `D` diagram).
  Search takes field prefixes (`col:email`, `type:decimal`, `is:view`, `ref:customer`, `code:throw`,
  `domain:order`); **In code** also searches inside definitions. Click any object to see its Definition,
  Columns, Relationships, Keys & indexes, Used by, Lineage and Impact (risk score + impact graph).
- **Diagram** — Graphviz ER diagram: tables and/or views, clustered by database, schema or domain,
  lineage tracing around a focused object, neighbourhood mode for large schemas, DOT/SVG download.
- **Dashboard** — inventory KPIs, top referenced / most connected tables, dependency hotspots,
  highest change risk, schemas, domains (renamable), cross-schema and cross-file counts, orphan tables.
- **Files** — per-script summary, which-script-depends-on-which matrix, foreign keys and code that
  cross files, and references to objects not in any uploaded script.

Click any row or chip in these views to jump to that object.
