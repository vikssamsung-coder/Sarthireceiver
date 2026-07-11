# Sarthi Dump Processor

Rebuilt to the approved design. One app to manage how the receiver handles every
dump: the Neon catalog of what can be sent, and — per type — how it's recognized,
where it's saved, and the sequence of code that runs. Your `email_processor.py`
still does the Outlook read, reassembly, SHA and dedup; this makes the
**recognition + per-dump sequence** data you edit on screen, not code.

## The four screens

- **Overview** — the pipeline every dump follows (Recognize → Save → Run → Record)
  and the latest runs.
- **Dump types** — a card per type: name, save folder, its step chips, active/off.
  Open one to configure it.
- **Configure** — three plain sections:
  1. **How it's recognized** — a form. Add conditions on **Sender / Subject /
     Body / Attachment / Anywhere**, choose **ALL or ANY**, and add more **rule
     groups** (groups are OR'd). "Sender is one of" takes a comma-separated list.
     A stamped label from PMD still routes automatically; these are the backup —
     and for plain-email watchers (Cube, NSE, trial balance…), these conditions
     *are* the recognition.
  2. **Save folder** — where the dump is copied before anything runs.
  3. **What runs, in order** — a numbered step list; add / reorder / delete;
     each step is a script or .bat, an arguments line, and stop/continue on fail.
- **Run history** — every dump handled and how each step went (the confirmation
  written back after each run).
- **Neon catalog** — sync the shared `dump_types` list; flags active types with
  no steps yet.

## Files

| file | what it is |
|---|---|
| `app.py` | the Streamlit app (the four screens). |
| `dump_flows.py` | registry: recognition + `resolve`, steps, folders, confirmations, catalog. |
| `extract.py` | auto-detects zip/csv/xlsx: unzips or places the dump into the folder. |
| `neon_sync.py` | reads the Neon URL from `secrets.toml` and syncs the catalog. |
| `flow_engine.py` | runs one dump end to end (extract → sequence → confirm). |
| `processor_integration.py` | the one-import + two-swap wiring into `email_processor.py`. |
| `test_app.py` | logic tests (recognition, resolve, args, secrets, end-to-end). |

## The dump can be zip, csv or xlsx

Before any step runs, the dump is normalised into the save folder by `extract.py`:

- **.zip** → unzipped into the folder (flattened; path-traversal is blocked).
- **.csv / .xlsx** → placed as-is (a real .xlsx is itself a zip, so it's *never*
  wrongly exploded — detection is by extension first).

The scripts then read the **extracted data file**, which the flow passes as
`{assembled_path}`. `{extract_dir}` is the folder. If a zip holds several files,
`{assembled_path}` points at the first csv/xlsx; all files are available in
`{extract_dir}` for a script that globs.

## Install & run (Sarthi box)

```
pip install streamlit pandas "psycopg[binary]"
cd D:\dump_processor_app\dump_processor_app
streamlit run app.py
```

- **Neon URL** is read from `D:\PMD-Desktop-main\.streamlit\secrets.toml`
  automatically — whatever the key is called (it finds the value that looks like
  a Postgres URL). `channel_binding=require` is stripped. Override with a
  `NEON_DATABASE_URL` env var if you prefer.
- First run: sidebar shows an empty registry → **Seed current 3 flows**, or go to
  **Neon catalog → Sync now**.

## Recognition, in plain terms

You build the rule; the app stores it as data. Examples:

- *Multiple senders*: Sender **is one of** `crm@bigul.co, orders@bigul.co`
- *Subject*: Subject **contains** `order file`
- *Both must hold*: put them in one group set to **ALL**
- *Either pattern*: **ANY**, or two **rule groups** for
  `(sender A AND subject X) OR (sender B AND subject Y)`
- *Pattern*: Subject **matches (regex)** `NSE_\d{4}`

Routing order: a stamped label (from PMD) wins first; otherwise each active type's
rules are checked in **Detect order** (lower first) and the first match wins.

## Wire into email_processor.py

See `processor_integration.py`: one import, swap `detect_dump_type` (now takes a
`sender` arg — pass `effective_sender` at the call site so sender-based rules
work), and swap `run_preprocessing_for_dump` for a one-line hand-off to
`flow_engine.run_dump_flow(...)` with your `run_python_script` / `run_bat`
injected. Nothing else changes; dedup, SHA and the dashboard-BAT gate are
untouched.
