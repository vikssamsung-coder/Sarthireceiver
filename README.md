# Sarthi Dump Processor

> **Project source of truth:** See [Complete Project Specification](docs/PROJECT_SPECIFICATION.md) for the full architecture, feature register, Client Intelligence data contracts, transaction taxonomy, AI optimization, security model, operations, testing, current status, known gaps, and roadmap.

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
- **Client Intelligence** — the integrated Customer Final Evaluation workspace. It refreshes Client 360, ingests evaluated-call files, creates stable call IDs, deduplicates and versions calls, builds the client-wise timeline, and generates the operational workbook.
- **New Client Profiling** — an operation inside Client Intelligence for current-and-previous-month or rolling-window accounts, with lead attribution, funds, brokerage, margin, top symbols, subscriptions and optional TPP analysis. TPP stays separate from the Client 360 used by call intelligence.

## Client Intelligence setup

The Receiver contains the pipeline; a separate `Sarthi_Evaluator` checkout is
not required for this section.

Fixed locations:

```text
Working folder : D:\Customer Final Evaluation
Leads CSV      : D:\Sarthi\Leads\Leads.csv
Call inputs    : D:\Customer Final Evaluation\01_Input\Call_Analysis
Client 360     : D:\Customer Final Evaluation\01_Input\Client_360
Current output : D:\Customer Final Evaluation\04_Output\Current
```

`Leads.csv` remains at its existing location and is read directly. Do not copy
it into the Customer Final Evaluation folder.

Available operations are allow-listed:

```text
SETUP · BUILD_360 · PROCESS_CALLS · FULL
```

Set `SARTHI_DB_PASSWORD` as a Windows environment variable before running
`BUILD_360` or `FULL`. Call-file processing does not require the MySQL
password. Place evaluated `.xlsx`, `.xlsm`, or `.csv` files in
`01_Input\Call_Analysis`, then run **Process evaluated call files**.

The Daily Run first derives the in-scope population from all call-analysis files,
maps Lead Numbers through `Leads.csv`, excludes leads without Client Codes, builds
the call-driven Client 360, and then generates all three current reports:

```text
D:\Customer Final Evaluation\04_Output\Current\RM_Action_Sheet_Current.xlsx
D:\Customer Final Evaluation\04_Output\Current\Management_Dashboard_Current.xlsx
D:\Customer Final Evaluation\04_Output\Current\Sarthi_Client_Intelligence_Current.xlsx
```

Each successful build also creates timestamped archive copies and an output
manifest containing the processing Run ID, generation time, row counts, and
completion/exception status. The RM report has exactly one primary row per Client
Code; the detailed action records remain available on its supporting sheet.

Phase 2 interprets each new or changed Sarthi 360-matched call using the relevant, privacy-minimised Client 360 facts and maintains permanent Interest, Requirement, and Issue ledgers plus one unified Action Worklist. Exact duplicates, unmatched calls, and unchanged source files never use AI. Corrected call versions deterministically rebuild current ledgers so superseded intelligence is removed. Set `OPENAI_API_KEY` for structured extraction; without it, calls are safely ingested and remain pending.

The evaluator uses a hybrid model policy by default: `gpt-5.6-luna` handles the first pass, while `gpt-5.6-terra` independently re-evaluates critical, complaint/churn-risk, recurring-outage, ambiguous, or low-confidence calls. Configure it with `SARTHI_AI_LUNA_MODEL`, `SARTHI_AI_TERRA_MODEL`, and `SARTHI_AI_TERRA_CONFIDENCE_THRESHOLD` (default `0.80`). Set `SARTHI_AI_HYBRID_ENABLED=false` to use the legacy single-model `SARTHI_AI_MODEL` path. The screen setting controls the AI batch size; one Daily Run automatically continues through all pending eligible batches, so the user does not need to start the process repeatedly.

Every model attempt is stored separately with response ID, token usage, processing time, status, escalation reason, and estimated USD cost. A failed Terra review falls back to the successful Luna extraction and remains visibly flagged for follow-up.

The editable Phase 2 AI prompt is stored separately at `client_intelligence_pipeline/prompts/phase2_call_intelligence.md`. Its content hash is recorded as part of the prompt version, so a prompt change safely queues eligible calls for re-interpretation instead of silently changing behaviour.

Issue closure is controlled: an internal resolution claim becomes `Resolved Pending Confirmation`; final closure requires client confirmation or system/transaction validation.

## Files

| file | what it is |
|---|---|
| `app.py` | the Streamlit app and navigation. |
| `app_client_intelligence.py` | Integrated Client Intelligence screen. |
| `customer_evaluation_adapter.py` | Fixed-path, allow-listed command adapter. |
| `client_intelligence_pipeline/` | Client 360 and common-call processing engine. |
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

For normal daily use, double-click:

`start_sarthi.bat`

The launcher uses its own folder automatically, checks/install missing
dependencies, verifies the integrated Client Intelligence pipeline and prompt,
then starts the Streamlit app and background Receiver/MIS services. No
PowerShell commands are required for routine startup.

In **Client Intelligence**, the default operation is **Daily Run - Refresh 360
and process calls**. Put new evaluated-call files in the fixed Call Analysis
folder and click **Start daily run**. The daily run also creates any missing
Customer Evaluation folders, so the separate setup operation is not required
before routine processing.

For first-time/manual startup:

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
