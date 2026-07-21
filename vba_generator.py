# -*- coding: utf-8 -*-
r"""
vba_generator.py — the app writes the Outlook VBA.

The app is the source of truth for mail routing. Each dump_types row carries its
recognition rules (recognition_json). This module translates ALL enabled types
into ONE complete Outlook .bas module that:

    on new mail (ItemAdd)
      -> for each feed, test its conditions (sender / subject / body / attachment)
      -> if matched: save the attachment to the drop folder
      -> call run_direct.py --enqueue  (drops a task on intake_queue)
      -> the app's intake worker extracts + runs the steps

The VBA does the MINIMUM: catch, save, enqueue. No pipeline logic lives in
Outlook anymore — change a folder or a step in the app, regenerate, paste once.

Workflow:
    App -> "VBA generator" screen -> Generate -> copy -> Outlook Alt+F11 ->
    paste over the SarthiDirectReceiver module -> save. Done until the next change.

Recognition JSON shape (same one df.resolve uses):
  {"groups": [
     {"mode": "all"|"any",
      "conditions": [
        {"field": "sender"|"subject"|"body"|"attachment"|"anywhere",
         "op": "contains"|"equals"|"is"|"is_one_of"|"matches",
         "value": "..."   OR   "values": ["...","..."]}
      ]}
  ]}
Groups are OR'd; conditions within a group follow its mode.
"""
from __future__ import annotations

import json
from datetime import datetime

import dump_flows as df

# Defaults — overridable from the generator screen.
DEFAULT_PYTHON = r"C:\Users\Vikrant.Dale\AppData\Local\Python\pythoncore-3.14-64\python.exe"
DEFAULT_RUNNER = r"D:\dump_processor_app\run_direct.py"
DEFAULT_DROP = r"D:\Sarthi\Incoming"
DEFAULT_LOG = r"D:\Sarthi\vba_intake_log.txt"


def _vqs(s: str) -> str:
    """Escape a Python string into a VBA double-quoted literal."""
    return '"' + str(s or "").replace('"', '""') + '"'


def _cond_to_vba(cond: dict) -> str:
    """One condition -> a VBA boolean expression over senderEmail/subjectText/
    bodyText/attachNames. Returns 'False' for anything untranslatable."""
    field = (cond.get("field") or "").lower()
    op = (cond.get("op") or "contains").lower()
    values = cond.get("values")
    if values is None:
        v = cond.get("value")
        values = [v] if v is not None else []
    values = [str(x) for x in values if str(x).strip()]
    if not values:
        return "False"

    var = {
        "sender": "senderEmail",
        "subject": "subjectText",
        "body": "bodyText",
        "attachment": "attachNames",
        "anywhere": "anywhereText",
    }.get(field, "anywhereText")

    parts = []
    for val in values:
        lv = val.lower()
        if op in ("equals", "is"):
            parts.append(f"({var} = {_vqs(lv)})")
        else:  # contains, is_one_of, matches(approx as contains)
            parts.append(f"(InStr({var}, {_vqs(lv)}) > 0)")
    return "(" + " Or ".join(parts) + ")"


def _group_to_vba(group: dict) -> str:
    conds = group.get("conditions") or []
    exprs = [_cond_to_vba(c) for c in conds]
    exprs = [e for e in exprs if e and e != "False"]
    if not exprs:
        return "False"
    joiner = " And " if (group.get("mode") or "all").lower() == "all" else " Or "
    return "(" + joiner.join(exprs) + ")"


def _rules_to_vba(recognition_json: str) -> str:
    """Whole rule tree -> a single VBA boolean. Groups are OR'd."""
    try:
        groups = json.loads(recognition_json or '{"groups":[]}').get("groups", [])
    except Exception:
        groups = []
    exprs = [_group_to_vba(g) for g in groups]
    exprs = [e for e in exprs if e and e != "False"]
    if not exprs:
        return "False"
    return " Or ".join(exprs)


def _watcher_sub(key: str, name: str, match_expr: str, dump_type: str) -> str:
    safe = "".join(ch for ch in key if ch.isalnum() or ch == "_") or "feed"
    return f'''
' ---- {name} ({key}) ----
Private Sub Watch_{safe}(ByVal mail As Outlook.MailItem, _
                         ByVal senderEmail As String, ByVal subjectText As String, _
                         ByVal bodyText As String, ByVal attachNames As String, _
                         ByVal anywhereText As String)
    On Error GoTo EH
    If Not ({match_expr}) Then Exit Sub
    WriteIntakeLog "MATCH {key} | sender=" & senderEmail & " | subj=" & subjectText
    EnqueueMail mail, {_vqs(dump_type)}
    Exit Sub
EH:
    WriteIntakeLog "ERR Watch_{safe}: " & Err.Number & " | " & Err.Description
End Sub'''


def generate(python_exe: str = DEFAULT_PYTHON, runner: str = DEFAULT_RUNNER,
             drop_folder: str = DEFAULT_DROP, log_path: str = DEFAULT_LOG,
             db_path=None, include_disabled: bool = False,
             multipart_keys=None) -> str:
    """Return the full .bas text. multipart_keys: dump-type keys whose feed sends
    SHA-stamped multipart parts (each part is enqueued; the app reassembles)."""
    types = df.list_dump_types(db_path) if db_path else df.list_dump_types()
    if not include_disabled:
        types = [t for t in types if t.get("enabled")]
    multipart_keys = set(multipart_keys or [])

    watchers, calls = [], []
    skipped = []
    for t in types:
        key = t["key"]
        expr = _rules_to_vba(t.get("recognition_json") or "")
        if expr == "False":
            skipped.append(key)
            continue
        watchers.append(_watcher_sub(key, t.get("name") or key, expr, key))
        safe = "".join(ch for ch in key if ch.isalnum() or ch == "_") or "feed"
        calls.append(f"    Call Watch_{safe}(mail, senderEmail, subjectText, "
                     f"bodyText, attachNames, anywhereText)")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    skip_note = ("' (no translatable rules, skipped: " + ", ".join(skipped) + ")"
                 if skipped else "")

    header = f'''Attribute VB_Name = "SarthiDirectReceiver"
'====================================================================
'  GENERATED BY THE PROCESSOR APP — do not hand-edit.
'  Generated: {ts}
'  Feeds:     {len(watchers)}    {skip_note}
'
'  This module ONLY catches mail and hands it to the app:
'     match -> save attachment to drop folder -> run_direct.py --enqueue
'  All extraction, folders and steps live in the app. To change routing,
'  edit the dump type in the app, regenerate, and paste over this module.
'
'  SETUP (once):
'    1. Alt+F11 -> import/replace this module.
'    2. Copy the two routines at the very bottom into ThisOutlookSession.
'    3. Tools > References -> Microsoft Outlook nn.n Object Library.
'    4. Trust Center -> enable macros; restart Outlook.
'====================================================================
Option Explicit

Private Const PYTHON_EXE As String = {_vqs(python_exe)}
Private Const RUNNER     As String = {_vqs(runner)}
Private Const DROP_FOLDER As String = {_vqs(drop_folder)}
Private Const INTAKE_LOG  As String = {_vqs(log_path)}

Private WithEvents InboxItems As Outlook.Items


Private Sub Application_Startup()
    On Error Resume Next
    Dim ns As Outlook.NameSpace
    Set ns = Application.GetNamespace("MAPI")
    Set InboxItems = ns.GetDefaultFolder(olFolderInbox).Items
    WriteIntakeLog "watcher activated — {len(watchers)} feed(s)"
End Sub


Private Sub InboxItems_ItemAdd(ByVal Item As Object)
    On Error GoTo EH
    If TypeName(Item) <> "MailItem" Then Exit Sub
    Dim mail As Outlook.MailItem: Set mail = Item

    Dim senderEmail As String, subjectText As String, bodyText As String
    Dim attachNames As String, anywhereText As String
    senderEmail = LCase(Trim(SenderSMTP(mail)))
    subjectText = LCase(mail.Subject & "")
    bodyText = LCase(mail.Body & "")
    attachNames = LCase(AttachmentNames(mail))
    anywhereText = senderEmail & " " & subjectText & " " & bodyText & " " & attachNames

    WriteIntakeLog "NEW | sender=" & senderEmail & " | subj=" & subjectText

{chr(10).join(calls) if calls else "    ' (no feeds configured)"}
    Exit Sub
EH:
    WriteIntakeLog "ERR ItemAdd: " & Err.Number & " | " & Err.Description
End Sub
'''

    core = '''

'====================================================================
'  CORE: save attachment(s) + enqueue a task in the app
'====================================================================
Private Sub EnqueueMail(ByVal mail As Outlook.MailItem, ByVal dumpKey As String)
    On Error GoTo EH
    Dim att As Outlook.Attachment, fpath As String, cmd As String
    Dim entryId As String, smtp As String, subj As String, didOne As Boolean

    If Len(Dir(DROP_FOLDER, vbDirectory)) = 0 Then MkDir DROP_FOLDER
    entryId = mail.EntryID
    smtp = LCase(Trim(SenderSMTP(mail)))
    subj = Replace(mail.Subject & "", """", "'")

    didOne = False
    For Each att In mail.Attachments
        ' skip tiny inline images
        If att.Size > 4096 Then
            fpath = DROP_FOLDER & "\\" & att.FileName
            att.SaveAsFile fpath
            cmd = """" & PYTHON_EXE & """ """ & RUNNER & """ --enqueue " & _
                  "--dump-type " & dumpKey & " " & _
                  "--file """ & fpath & """ " & _
                  "--subject """ & subj & """ " & _
                  "--sender """ & smtp & """ " & _
                  "--entry-id """ & entryId & att.FileName & """"
            Shell "cmd /c " & cmd, vbHide
            WriteIntakeLog "  queued " & dumpKey & " <- " & att.FileName
            didOne = True
        End If
    Next

    ' No attachment (e.g. a body-only trigger): still enqueue a task with no file.
    If Not didOne Then
        cmd = """" & PYTHON_EXE & """ """ & RUNNER & """ --enqueue " & _
              "--dump-type " & dumpKey & " " & _
              "--file ""(none)"" " & _
              "--subject """ & subj & """ " & _
              "--sender """ & smtp & """ " & _
              "--entry-id """ & entryId & """"
        Shell "cmd /c " & cmd, vbHide
        WriteIntakeLog "  queued " & dumpKey & " (no attachment)"
    End If
    Exit Sub
EH:
    WriteIntakeLog "ERR EnqueueMail(" & dumpKey & "): " & Err.Number & " | " & Err.Description
End Sub


Private Function AttachmentNames(ByVal mail As Outlook.MailItem) As String
    On Error Resume Next
    Dim att As Outlook.Attachment, s As String
    For Each att In mail.Attachments
        s = s & att.FileName & " "
    Next
    AttachmentNames = s
End Function


Private Function SenderSMTP(ByVal mail As Outlook.MailItem) As String
    On Error GoTo FB
    If mail.SenderEmailType = "EX" Then
        Dim u As Outlook.ExchangeUser
        Set u = mail.Sender.GetExchangeUser
        If Not u Is Nothing Then SenderSMTP = u.PrimarySmtpAddress
        If Len(SenderSMTP) = 0 Then
            SenderSMTP = mail.PropertyAccessor.GetProperty( _
                "http://schemas.microsoft.com/mapi/proptag/0x39FE001E")
        End If
    Else
        SenderSMTP = mail.SenderEmailAddress
    End If
    Exit Function
FB:
    SenderSMTP = mail.SenderEmailAddress & ""
End Function


Private Sub WriteIntakeLog(ByVal t As String)
    On Error Resume Next
    Dim fso As Object, f As Object, folder As String
    Set fso = CreateObject("Scripting.FileSystemObject")
    folder = fso.GetParentFolderName(INTAKE_LOG)
    If Len(folder) > 0 Then If Not fso.FolderExists(folder) Then fso.CreateFolder folder
    Set f = fso.OpenTextFile(INTAKE_LOG, 8, True)
    f.WriteLine Format(Now, "yyyy-mm-dd hh:nn:ss") & " | " & t
    f.Close
End Sub
'''

    footer = '''

'====================================================================
'  COPY THESE TWO INTO  ThisOutlookSession  (not this module):
'====================================================================
'
'   Private WithEvents inboxItems As Outlook.Items
'
'   Private Sub Application_Startup()
'       Dim ns As Outlook.NameSpace
'       Set ns = Application.GetNamespace("MAPI")
'       Set inboxItems = ns.GetDefaultFolder(olFolderInbox).Items
'   End Sub
'
'   Private Sub inboxItems_ItemAdd(ByVal Item As Object)
'       SarthiDirectReceiver.RouteFromSession Item
'   End Sub
'
'====================================================================
'  (RouteFromSession lets the module own the logic; ThisOutlookSession
'   just forwards the event.)
Public Sub RouteFromSession(ByVal Item As Object)
    On Error Resume Next
    InboxItems_ItemAdd Item
End Sub
'''

    return header + "\n".join(watchers) + core + footer


if __name__ == "__main__":
    print(generate())
