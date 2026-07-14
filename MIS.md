# MIS Builder

Reports built on the Sarthi box and emailed out. Same registry, same app, same
discipline as the dump side: **flows are data**. A new report is config + one
build script, never receiver code.

## Three triggers, one queue

    PMD request (Neon report_requests) ─┐
    Schedule (mis_types.schedule_time) ─┼──▶ mis_queue ──▶ mis_engine ──▶ email
    Dump landed (flow_engine hook) ─────┘   (dedupe +      (steps in     (Outlook)
                                             claim)         order)

Three producers, ONE single-threaded worker. A 09:00 schedule and a dump landing
at 09:00 cannot build the same report twice — `claim_next()` skips any report
already in flight.

## Strict sequence — three separate guarantees

1. **Within a run** — steps execute in `step_order`, one subprocess at a time.
   A failing `stop` step aborts immediately, so no later step ever sees a
   half-built input.
2. **Across runs of one report** — serialised by the claim.
3. **The worker** — single thread. Deliberate. Throughput is not the problem here.

## The one new contract

A build step **must print `OUTPUT=<absolute path>`** on stdout. The engine takes
the last such line from the run and emails that file.

**No `OUTPUT=` line -> the run FAILS.** Not "newest file in the folder" — build
scripts write temp and intermediate files, and that heuristic silently mails the
wrong one. See `sample_build_step.py`.

## Triggers in detail

**Schedule** — `HH:MM` + a Mon..Sun bit mask. **Fires regardless of lateness**:
if the box was off at 09:00, the report goes out when the poller comes back up.
It is never silently skipped. (To change that, add a max-age check in
`mis_triggers._due()` — that is the only place.)

**After dumps** — `{"mode": "all", "keys": ["orderbook", "trial_balance"]}`
- `all` (default): waits until EVERY listed dump has a **success** in `flow_runs`
  today. The last one to land fires the build. Once per day.
- `any`: fires on the first one.

A `partial` dump run (a step failed) does **not** feed a report. The data is
suspect.

## Recipients

| trigger | goes to |
|---|---|
| request | `report_requests.requester_email` |
| schedule / dump / manual | Neon `mis_report_access`, expanded against `users` |

`mis_report_access(report_key, principal_type, principal)`. Handled principal
types: `user`, `role` (matches `role` OR `login_role`), `department`, `email`,
`all`. Active users only.

- **No access rows** -> `admin_emails` from secrets.toml. Deny by default.
- **Rows that match nobody** -> **error**, not an admin fallback. A rule matching
  no one is a typo, not an instruction to reroute the report.
- **No admins configured either** -> the run FAILS. It never reports success on a
  report nobody received.

Add to `D:\PMD-Desktop-main\.streamlit\secrets.toml`:

    admin_emails = "vikrant@bigul.co"

Check it end to end before trusting a schedule:

    python mis_neon.py

That prints the secrets path, the Neon host, whether `channel_binding` was
stripped, which `principal_type` values you actually use, and the resolved
recipient list for **every** report.

## Files

| file | owns |
|---|---|
| `mis_flows.py` | Registry: mis_types / mis_flow_steps / mis_queue / mis_runs. Pure Python. Imports `DEFAULT_DB` from `dump_flows` — the path is defined once. |
| `mis_engine.py` | Runs ONE build: steps in order, OUTPUT= contract. COM-free. Reuses `dump_flows._render_args`. |
| `mis_triggers.py` | Schedule ticker + dump-complete hook. |
| `mis_neon.py` | report_requests poll/claim + principal expansion. Reuses `neon_sync.load_neon_url`. |
| `mis_mailer.py` | Outlook COM. The only file that touches it. |
| `mis_poller.py` | The MIS process: producers + single-threaded worker. |
| `app_mis.py` | The MIS screens in `app.py`. |
| `mis_build_from_dumps.py` | A real build step reading your already-saved dumps. |
| `sample_build_step.py` | The step contract, one page. |
| `sarthi_service.py` | One window: receiver + MIS poller, with restart. |
| `test_mis.py` | 42 checks against the real modules. |

## What changed in existing files

Two hunks. Nothing else.

- **`flow_engine.py`** — after a **success** (not `partial`), calls
  `mis_triggers.on_dump_complete()`. Enqueue only, wrapped in try/except: an MIS
  problem can never break a dump.
- **`app.py`** — imports `mis_flows` + `app_mis`, calls `mf.init_db`, adds
  `"MIS reports"` and `"MIS history"` to `NAV`, adds three `elif` screens.
  `DB_PATH` now comes from `df.DEFAULT_DB` instead of being re-typed.

## Run

    run_sarthi.bat                          receiver + MIS, one window
    python mis_poller.py --once             one pass, then exit
    python mis_poller.py --build daily_mis  force a build now
    streamlit run app.py                    the app, now with MIS screens

Task Scheduler starts `run_sarthi.bat` **at boot only**. It does not own the fire
times — the schedule lives in `mis_types` and is edited in the app.

## Rollback

Revert the two hunks, delete `mis_*.py` and `app_mis.py`. The four `mis_*` tables
sit unused. MIS only ever **reads** `dump_types` and `flow_runs` — nothing on the
dump side is affected.

---

## Launching

**`streamlit run app.py` is the only thing you run.**

The app starts `sarthi_service.py` behind it — the Outlook receiver and the MIS
poller — as one detached background process. The sidebar shows `● Receiver & MIS
running`. The **Services** screen has status, the live log, and Start/Stop/Restart.

`service_manager.py` holds a PID lock beside the registry. Streamlit re-runs the
whole script on every click, so `ensure_running()` is called constantly — it
checks the lock, confirms the pid is alive AND is actually ours (Windows recycles
pids), and does nothing if so. Duplicates are impossible.

The services **outlive the app**: close the browser or the Streamlit terminal and
mail keeps being polled, schedules keep firing. That is deliberate — the whole
point is that a report goes out at 10:45 whether or not anyone has the app open.
Stop them from the Services screen when you actually want them down.

`run_sarthi.bat` still exists for a headless box (Task Scheduler at boot, no app).
Both paths are safe: whichever starts first takes the lock, the other sees it.

## When a schedule doesn't fire

    python mis_why.py

Tells you, per report: whether anything is watching the clock, whether today's day
bit is set, whether the slot has passed, whether it already fired, which dumps have
landed, and when it last queued and last ran.

The MIS reports screen also shows a red banner when a report is past due and still
not queued — which always means no poller is running.
