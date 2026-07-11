# Outlook watcher (VBA) — what changes

Short version: the watcher gets **simpler**. It no longer runs any processing
scripts. Its only job now is: **catch the email → save the attachment → hand it
to `run_direct.py`.** Everything else (which type it is, where it's saved, which
scripts run, unzipping, recording the result) is configured in the app and done
by the flow engine.

## What to remove
- Any code in the watcher that runs the per-dump scripts (LeadSquared / partner /
  cube / NSE etc.). That sequence now lives in the app under the dump type's
  **What runs, in order**.

## What to add
One call per caught email to the entry point:

```
python "D:\dump_processor_app\run_direct.py" --file "<saved attachment>" --subject "<subject>" --sender "<smtp address>"
```

`run_direct.py` resolves the dump type from the **identifier rules** you set in
the app (sender / subject / body), then runs that type's flow and records it in
Run history. If a watcher already knows what it caught, skip recognition and name
it: add `--dump-type cube_calllog`.

## Drop-in VBA

```vba
' Resolve the real SMTP address (Exchange senders return an X.500 DN otherwise,
' which won't match a "Sender is one of a@bigul.co" rule).
Function SenderSMTP(mail As Outlook.MailItem) As String
    On Error Resume Next
    If mail.SenderEmailType = "EX" Then
        SenderSMTP = mail.Sender.GetExchangeUser().PrimarySmtpAddress
        If Len(SenderSMTP) = 0 Then
            SenderSMTP = mail.PropertyAccessor.GetProperty( _
                "http://schemas.microsoft.com/mapi/proptag/0x39FE001E") ' PR_SMTP_ADDRESS
        End If
    Else
        SenderSMTP = mail.SenderEmailAddress
    End If
End Function

' Call this for each matching mail (from ItemAdd, or a "run a script" rule).
Public Sub HandleDirectMail(Item As Outlook.MailItem)
    Const DROP As String = "D:\Sarthi\Incoming\"
    Const RUNNER As String = "D:\dump_processor_app\run_direct.py"
    Dim att As Outlook.Attachment, fpath As String, cmd As String, smtp As String

    If Item.Attachments.Count = 0 Then Exit Sub
    If Len(Dir(DROP, vbDirectory)) = 0 Then MkDir DROP
    smtp = SenderSMTP(Item)

    For Each att In Item.Attachments
        fpath = DROP & att.FileName
        att.SaveAsFile fpath
        cmd = "python """ & RUNNER & """ " & _
              "--file """ & fpath & """ " & _
              "--subject """ & Replace(Item.Subject, """", "'") & """ " & _
              "--sender """ & smtp & """"
        ' add:  & " --dump-type cube_calllog"   to skip recognition
        Shell "cmd /c " & cmd, vbHide
    Next
End Sub
```

Wire `HandleDirectMail` to the inbox `ItemAdd` event (in `ThisOutlookSession`),
or run it from an Outlook rule's "run a script" action.

## Gotchas
- **Sender must be SMTP.** Exchange `SenderEmailAddress` is often an X.500 DN
  (`/o=.../cn=...`), which won't match your `a@bigul.co` rules. The `SenderSMTP`
  helper above fixes that. (Or dodge it entirely by passing `--dump-type`.)
- **Python on PATH.** If `python` isn't found, use the full path to `python.exe`.
- **`run_direct.py` location.** It must sit in the app folder next to
  `dump_flows.py`, `flow_engine.py`, `extractor.py`, and point at the same DB
  (`D:\Sarthi\multipart_buffer\dump_flows.sqlite3`) — that's the default.
- **No de-dup on this path.** `email_processor.py` de-dupes PMD dumps; the VBA
  path doesn't. If your watcher can fire twice for one mail, move or delete the
  saved file after handing off, or add a "processed" marker, so it runs once.
- **PMD dumps are unaffected.** Those still flow through `email_processor.py`
  (the `[CDP MULTIPART]` handler). This is only for the direct, non-PMD emails.
```
