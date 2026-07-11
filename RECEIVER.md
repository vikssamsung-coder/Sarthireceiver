# Sarthi Receiver (standalone)

This is the self-contained receiver: it reads the Outlook inbox itself and does
the whole job through the registry you set up in the app.

```
new mail  ->  recognize the dump type  ->  save the attachment
          ->  extract (zip unzips; csv/xlsx as-is) into the type's folder
          ->  run the type's steps in order  ->  record the result
```

Everything shows up in the app's **Run history**. It doesn't depend on the old
`email_processor.py` — that can keep running in parallel and fade out.

## Run it

```
run_receiver.bat            # keeps polling every 60s (double-click; leave open)
```

or from a prompt:

```
python sarthi_receiver.py --once                  # one pass (for Task Scheduler)
python sarthi_receiver.py --watch --interval 60   # continuous
python sarthi_receiver.py --mailbox "growth@bigul.co" --folder "Inbox" --scan 50
```

Needs `pywin32` (the bat installs it on first run). Reads the registry DB at
`D:\Sarthi\multipart_buffer\dump_flows.sqlite3` and tracks handled mail in
`receiver_seen.sqlite3` next to it, so nothing is processed twice.

## How a mail is matched

Same rules as the app:
1. If the mail carries a PMD **label** (`dump_type_handler` / `dump_type_key`),
   that routes it — sender can be anyone.
2. Otherwise the **identifier rules** you set (sender / subject / body) decide.
3. No match -> skipped and remembered, so it isn't re-checked forever.

## Scope / what it does NOT do yet

- **Single-attachment emails.** It saves each mail's attachment and processes it
  (the extractor unzips a `.zip`, or passes a `.csv`/`.xlsx` straight through).
- **It does not reassemble `[CDP MULTIPART]` split dumps.** PMD can send a large
  dump across several part-emails; stitching those back together (with the SHA
  checks) is what the old `email_processor.py` does. Until that's ported here,
  let the old processor keep handling the multipart PMD dumps — overlap is fine.
  If/when PMD dumps arrive as a single attachment, this receiver handles them by
  their label.

## Set up a dump type (in the app)

1. Create the type (or **Sync from Neon**), set **Comes from** (PMD or Direct).
2. Set its **save folder**.
3. **Direct** types: set the identifier (sender/subject). **PMD** types: routed
   by label automatically.
4. Add its **steps** in order.

Then start the receiver and send a test mail — watch it land in Run history.
