# Schema Reader (Streamlit)

Read a database from its DDL script — no database connection, nothing executed.
Upload the `.sql` files produced by SSMS *Generate Scripts*, `pg_dump`, `mysqldump`
or Oracle DDL and explore objects, relationships, lineage, change impact and an ER diagram.

## Files

| File | Purpose |
|---|---|
| `app.py` | The Streamlit app (entry point) |
| `schema_engine.py` | Parser and analysis engines (no Streamlit dependency) |
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

- **Explorer** — drill down by Database › Schema › Type (or Schema, Type, Domain, File, Flat),
  search with field prefixes (`col:email`, `type:decimal`, `is:view`, `ref:customer`, `code:throw`,
  `domain:order`), and open any object to see its Definition, Columns, Relationships, Keys & indexes,
  Used by, Lineage and Impact (risk score + impact graph). Favourites and recently viewed are pinned at the top.
- **Diagram** — Graphviz ER diagram: tables and/or views, clustered by database, schema or domain,
  lineage tracing around a focused object, neighbourhood mode for large schemas, DOT/SVG download.
- **Dashboard** — inventory KPIs, top referenced / most connected tables, dependency hotspots,
  highest change risk, schemas, domains (renamable), cross-schema and cross-file counts, orphan tables.
- **Files** — per-script summary, which-script-depends-on-which matrix, foreign keys and code that
  cross files, and references to objects not in any uploaded script.

Click any row or chip in these views to jump to that object.
