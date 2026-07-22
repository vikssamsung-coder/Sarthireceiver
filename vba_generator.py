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
    }.get(field)
    if not var:
        return "False"

    parts = []
    for val in values:
        lv = val.lower()
        if op in ("equals", "is", "is_one_of"):
            parts.append(f"({var} = {_vqs(lv)})")
        elif op == "matches":
            parts.append(f"RegexMatch({var}, {_vqs(val)})")
        elif op == "contains":
            parts.append(f"(InStr({var}, {_vqs(lv)}) > 0)")
        else:
            parts.append("False")
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
Private Function Watch_{safe}(ByVal mail As Outlook.MailItem, _
                         ByVal senderEmail As String, ByVal subjectText As String, _
                         ByVal bodyText As String, ByVal attachNames As String, _
                         ByVal anywhereText As String) As Boolean
    On Error GoTo EH
    If Not ({match_expr}) Then Exit Function
    Watch_{safe} = True
    WriteIntakeLog "MATCH {key} | sender=" & senderEmail & " | subj=" & subjectText
    EnqueueMail mail, {_vqs(dump_type)}
    Exit Function
EH:
    WriteIntakeLog "ERR Watch_{safe}: " & Err.Number & " | " & Err.Description
End Function'''


def _condition_signature(cond: dict):
    """Hashable signature used for conservative overlap warnings."""
    field = str(cond.get("field") or "").lower()
    op = str(cond.get("op") or "contains").lower()
    vals = cond.get("values")
    if vals is None:
        vals = [cond.get("value")]
    vals = tuple(sorted(str(v).strip().lower() for v in vals if str(v or "").strip()))
    return field, op, vals


def _all_groups(recognition_json: str) -> list[set] | None:
    """Return AND-condition sets, or None for rule shapes we cannot prove."""
    try:
        groups = json.loads(recognition_json or "{}").get("groups", [])
    except (TypeError, json.JSONDecodeError):
        return None
    out = []
    for group in groups:
        if str(group.get("mode") or "all").lower() != "all":
            return None
        conds = {_condition_signature(c) for c in group.get("conditions") or []}
        if conds:
            out.append(conds)
    return out or None


def _overlap_warnings(types: list[dict]) -> list[str]:
    """Warn when a later rule provably contains an earlier first-match rule."""
    warnings = []
    for i, earlier in enumerate(types):
        earlier_groups = _all_groups(earlier.get("recognition_json") or "")
        if not earlier_groups:
            continue
        for later in types[i + 1:]:
            later_groups = _all_groups(later.get("recognition_json") or "")
            if not later_groups:
                continue
            # If every earlier alternative includes a later alternative's
            # conditions, every mail matched by earlier also matches later.
            if all(any(lg <= eg for lg in later_groups) for eg in earlier_groups):
                warnings.append(
                    f"Rule overlap: {later['key']} includes messages matched by "
                    f"higher-priority {earlier['key']}.")
    return warnings


def validation_report(db_path=None) -> dict:
    """Classify every feed before code generation.

    ``sort_order`` is the configurable routing priority: lower numbers are
    emitted first and first match wins.
    """
    types = df.list_dump_types(db_path) if db_path else df.list_dump_types()
    rows, routable = [], []
    for t in types:
        key = t["key"]
        try:
            parsed = json.loads(t.get("recognition_json") or '{"groups":[]}')
            valid_json = isinstance(parsed, dict) and isinstance(parsed.get("groups", []), list)
        except (TypeError, json.JSONDecodeError):
            valid_json = False
        has_rules = valid_json and _rules_to_vba(t.get("recognition_json") or "") != "False"
        try:
            has_steps = bool(df.get_steps(key, db_path) if db_path else df.get_steps(key))
        except Exception:
            has_steps = False

        if not t.get("enabled"):
            state = "Disabled"
        elif not valid_json:
            state = "Invalid configuration"
        elif has_rules and has_steps:
            state = "Active and routable"
            routable.append(t)
        elif has_rules:
            state = "Has routing rule but no processing steps"
        elif has_steps:
            state = "Has processing steps but no routing rule"
        else:
            state = "Invalid configuration"
        rows.append({"key": key, "priority": int(t.get("sort_order") or 100),
                     "state": state, "has_rules": has_rules, "has_steps": has_steps})
    return {"feeds": rows, "warnings": _overlap_warnings(routable)}


def generate(python_exe: str = DEFAULT_PYTHON, runner: str = DEFAULT_RUNNER,
             drop_folder: str = DEFAULT_DROP, log_path: str = DEFAULT_LOG,
             db_path=None, include_disabled: bool = False,
             multipart_keys=None, all_in_one: bool = False) -> str:
    """Return the full .bas text. multipart_keys: dump-type keys whose feed sends
    SHA-stamped multipart parts (each part is enqueued; the app reassembles)."""
    all_types = df.list_dump_types(db_path) if db_path else df.list_dump_types()
    types = list(all_types)
    if not include_disabled:
        types = [t for t in types if t.get("enabled")]
    multipart_keys = set(multipart_keys or [])

    watchers, calls = [], []
    skipped_norules = []
    nostep_warn = []
    invalid_rules = []
    routable_types = []
    for t in types:
        key = t["key"]

        # Both PMD and direct dumps arrive via Outlook — PMD with prefilled/
        # standard details, direct with feed-specific keywords. Either way it's
        # the recognition rules that match them, so we watch any feed that HAS
        # rules, regardless of origin. A feed with no rules can't be matched.
        raw_rules = t.get("recognition_json") or ""
        try:
            parsed = json.loads(raw_rules or '{"groups":[]}')
            if not isinstance(parsed, dict) or not isinstance(parsed.get("groups", []), list):
                raise ValueError("recognition_json must contain a groups list")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            invalid_rules.append(f"{key} ({exc})")
            continue
        expr = _rules_to_vba(raw_rules)
        if expr == "False":
            skipped_norules.append(key)
            continue

        # A feed with rules but no steps will catch mail and then fail in the
        # flow. Flag it so it's visible in the generated header.
        try:
            n_steps = len(df.get_steps(key, db_path) if db_path else df.get_steps(key))
        except Exception:
            n_steps = -1
        if n_steps == 0:
            nostep_warn.append(key)
            # Do not generate a route that can only create failed queue jobs.
            continue

        watchers.append(_watcher_sub(key, t.get("name") or key, expr, key))
        routable_types.append(t)
        safe = "".join(ch for ch in key if ch.isalnum() or ch == "_") or "feed"
        calls.append(f"    If Watch_{safe}(mail, senderEmail, subjectText, "
                     f"bodyText, attachNames, anywhereText) Then Exit Sub")

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notes = []
    if skipped_norules:
        notes.append("HAS STEPS/ACTIVE BUT NO ROUTING RULE: " + ", ".join(skipped_norules))
    if nostep_warn:
        notes.append("BLOCKED - HAS ROUTING RULE BUT NO STEPS: " + ", ".join(nostep_warn))
    if invalid_rules:
        notes.append("INVALID CONFIGURATION: " + "; ".join(invalid_rules))
    disabled = [t["key"] for t in all_types if not t.get("enabled")]
    if disabled:
        notes.append("DISABLED: " + ", ".join(disabled))
    notes.extend(_overlap_warnings(routable_types))
    suspicious = []
    for t in all_types:
        if "trail balance" in str(t.get("recognition_json") or "").lower():
            suspicious.append(f"{t['key']}: review 'trail balance' vs 'trial balance'")
    if suspicious:
        notes.append("SUSPICIOUS PHRASE: " + "; ".join(suspicious))
    skip_note = ""
    if notes:
        skip_note = "\n'  " + "\n'  ".join(notes)

    module_attribute = "" if all_in_one else 'Attribute VB_Name = "SarthiDirectReceiver"\n'
    header = f'''{module_attribute}'====================================================================
'  GENERATED BY THE PROCESSOR APP -- do not hand-edit.
'  Generated: {ts}
'  Active and routable feeds: {len(watchers)}{skip_note}
'
'  This module ONLY catches mail and hands it to the app:
'     match -> save attachment to drop folder -> run_direct.py --enqueue
'  All extraction, folders and steps live in the app. To change routing,
'  edit the dump type in the app, regenerate, and paste over this module.
'
'  SETUP (once):
'    1. Alt+F11.
'    2. {'Paste this entire block into ThisOutlookSession.' if all_in_one else 'Import/paste this module as SarthiDirectReceiver.'}
'    3. {'Run InitializeInboxWatcher once after pasting.' if all_in_one else 'Paste the event block from the bottom into ThisOutlookSession.'}
'    4. Tools > References -> tick Microsoft Outlook nn.n Object Library.
'    5. Trust Center -> enable macros; save; restart Outlook.
'====================================================================
Option Explicit

Private Const PYTHON_EXE As String = {_vqs(python_exe)}
Private Const RUNNER     As String = {_vqs(runner)}
Private Const DROP_FOLDER As String = {_vqs(drop_folder)}
Private Const INTAKE_LOG  As String = {_vqs(log_path)}

{'Private WithEvents inboxItems As Outlook.Items' if all_in_one else ''}


' Called by ThisOutlookSession's ItemAdd event (see the bottom of this file).
' All routing lives here; ThisOutlookSession just forwards the new-mail event.
Public Sub RouteMail(ByVal Item As Object)
    On Error GoTo EH
    If Item Is Nothing Then Exit Sub
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
    WriteIntakeLog "NO MATCH | sender=" & senderEmail & " | subj=" & subjectText
    Exit Sub
EH:
    WriteIntakeLog "ERR RouteMail: " & Err.Number & " | " & Err.Description
End Sub


' Safe manual test. Never run inboxItems_ItemAdd directly: Outlook supplies its
' Item argument only when the event fires.
Public Sub TestSelectedEmail()
    On Error GoTo EH
    Dim sel As Outlook.Selection
    Set sel = Application.ActiveExplorer.Selection
    If sel Is Nothing Then
        MsgBox "Select one email in Outlook first.", vbInformation
        Exit Sub
    End If
    If sel.Count = 0 Then
        MsgBox "Select one email in Outlook first.", vbInformation
        Exit Sub
    End If
    If TypeName(sel.Item(1)) <> "MailItem" Then
        MsgBox "The selected item is not an email.", vbExclamation
        Exit Sub
    End If
    RouteMail sel.Item(1)
    Exit Sub
EH:
    MsgBox "TestSelectedEmail error " & Err.Number & ": " & Err.Description, vbExclamation
End Sub
'''

    core = '''

'====================================================================
'  CORE: save attachment(s) + enqueue a task in the app
'====================================================================
Private Sub EnqueueMail(ByVal mail As Outlook.MailItem, ByVal dumpKey As String)
    On Error GoTo EH
    Dim att As Outlook.Attachment, fpath As String, cmd As String, jobFile As String
    Dim entryId As String, entryKey As String, smtp As String, didOne As Boolean
    Dim token As String, originalName As String

    EnsureFolder DROP_FOLDER
    entryId = mail.EntryID
    smtp = LCase(Trim(SenderSMTP(mail)))

    didOne = False
    For Each att In mail.Attachments
        ' Keep valid data files regardless of size; exclude signature images by
        ' extension instead of dropping every attachment under 4 KB.
        If IsDataAttachment(att) Then
            token = UniqueToken(mail, att.Index)
            originalName = SafeFileName(att.FileName)
            fpath = DROP_FOLDER & "\\" & token & "_" & originalName
            att.SaveAsFile fpath
            entryKey = entryId & ":" & CStr(att.Index)
            jobFile = WriteJobFile(token, fpath, originalName, mail.Subject & "", _
                                   smtp, entryKey, dumpKey)
            cmd = """" & PYTHON_EXE & """ """ & RUNNER & _
                  """ --job-file """ & jobFile & """"
            RunAndLog cmd, dumpKey, originalName
            didOne = True
        End If
    Next

    ' No attachment (e.g. a body-only trigger): still enqueue a task with no file.
    If Not didOne Then
        token = UniqueToken(mail, 0)
        jobFile = WriteJobFile(token, "", "", mail.Subject & "", smtp, _
                               entryId & ":0", dumpKey)
        cmd = """" & PYTHON_EXE & """ """ & RUNNER & _
              """ --job-file """ & jobFile & """"
        RunAndLog cmd, dumpKey, "(no attachment)"
    End If
    Exit Sub
EH:
    WriteIntakeLog "ERR EnqueueMail(" & dumpKey & "): " & Err.Number & " | " & Err.Description
End Sub


' Run the enqueue command and PROVE it launched. Writes the command to a .cmd
' wrapper that appends its own output to the log, so a bad Python path or a
' run_direct error is visible instead of a silent "queued" that never lands.
Private Sub RunAndLog(ByVal cmd As String, ByVal dumpKey As String, ByVal what As String)
    On Error GoTo EH
    Dim wsh As Object, rc As Long, full As String
    ' verify the python exe exists first
    If Len(Dir(PYTHON_EXE)) = 0 Then
        WriteIntakeLog "  !! PYTHON not found: " & PYTHON_EXE & " (fix PYTHON_EXE and regenerate)"
        Exit Sub
    End If
    If Len(Dir(RUNNER)) = 0 Then
        WriteIntakeLog "  !! run_direct.py not found: " & RUNNER
        Exit Sub
    End If
    ' WScript.Shell.Run with bWaitOnReturn=True returns the exit code, so we
    ' KNOW whether it worked (Shell() alone can't tell us).
    Set wsh = CreateObject("WScript.Shell")
    ' cd into the app folder FIRST so run_direct.py and its imports resolve.
    ' Outlook's working dir is the user profile, which is why a bare Shell fails.
    full = "cmd /c cd /d """ & AppDir() & """ && " & cmd & _
           " >> """ & INTAKE_LOG & """ 2>&1"
    rc = wsh.Run(full, 0, True)
    Select Case rc
        Case 0
            WriteIntakeLog "QUEUED " & dumpKey & " <- " & what
        Case 2
            WriteIntakeLog "DUPLICATE skipped " & dumpKey & " <- " & what
        Case Else
            WriteIntakeLog "FAILED exit=" & rc & " " & dumpKey & " <- " & what
    End Select
    Exit Sub
EH:
    WriteIntakeLog "  !! RunAndLog error " & Err.Number & " | " & Err.Description
End Sub


Private Function AppDir() As String
    ' The folder run_direct.py lives in (parent of RUNNER).
    Dim p As Long
    p = InStrRev(RUNNER, "\\")
    If p > 0 Then
        AppDir = Left(RUNNER, p - 1)
    Else
        AppDir = "D:\\dump_processor_app"
    End If
End Function


Private Function AttachmentNames(ByVal mail As Outlook.MailItem) As String
    On Error GoTo EH
    Dim att As Outlook.Attachment, s As String
    For Each att In mail.Attachments
        s = s & att.FileName & " "
    Next
    AttachmentNames = s
    Exit Function
EH:
    Debug.Print "AttachmentNames error " & Err.Number & ": " & Err.Description
End Function


Private Function IsDataAttachment(ByVal att As Outlook.Attachment) As Boolean
    On Error GoTo EH
    Dim p As Long, ext As String
    p = InStrRev(LCase(att.FileName), ".")
    If p = 0 Then Exit Function
    ext = Mid$(LCase(att.FileName), p)
    Select Case ext
        Case ".csv", ".xlsx", ".xls", ".xlsb", ".txt", ".json", _
             ".zip", ".gz", ".pdf", ".tsv"
            IsDataAttachment = True
    End Select
    Exit Function
EH:
    Debug.Print "IsDataAttachment error " & Err.Number & ": " & Err.Description
End Function


Private Function SafeFileName(ByVal value As String) As String
    Dim bad As Variant, ch As Variant
    bad = Array("\\", "/", ":", "*", "?", """", "<", ">", "|")
    SafeFileName = value
    For Each ch In bad
        SafeFileName = Replace(SafeFileName, CStr(ch), "_")
    Next ch
End Function


Private Function UniqueToken(ByVal mail As Outlook.MailItem, ByVal attachmentIndex As Long) As String
    Dim suffix As String
    suffix = Right$("000000000000" & mail.EntryID, 12)
    suffix = SafeFileName(suffix)
    UniqueToken = Format(mail.ReceivedTime, "yyyymmdd_hhnnss") & "_" & _
                  suffix & "_" & CStr(attachmentIndex)
End Function


Private Function JsonEscape(ByVal value As String) As String
    value = Replace(value, "\\", "\\\\")
    value = Replace(value, """", "\\""")
    value = Replace(value, vbCr, "\\r")
    value = Replace(value, vbLf, "\\n")
    value = Replace(value, vbTab, "\\t")
    JsonEscape = value
End Function


Private Function WriteJobFile(ByVal token As String, ByVal filePath As String, _
                              ByVal originalName As String, ByVal subject As String, _
                              ByVal sender As String, ByVal entryId As String, _
                              ByVal dumpKey As String) As String
    On Error GoTo EH
    Dim fso As Object, folder As String, path As String, payload As String
    Set fso = CreateObject("Scripting.FileSystemObject")
    folder = DROP_FOLDER & "\\jobs"
    EnsureFolder folder
    path = folder & "\\" & token & ".json"
    payload = "{""enqueue"":true,""delete_after_read"":true," & _
              """file"":""" & JsonEscape(filePath) & """," & _
              """original_filename"":""" & JsonEscape(originalName) & """," & _
              """subject"":""" & JsonEscape(subject) & """," & _
              """sender"":""" & JsonEscape(sender) & """," & _
              """entry_id"":""" & JsonEscape(entryId) & """," & _
              """dump_type"":""" & JsonEscape(dumpKey) & """}"
    WriteUtf8 path, payload
    WriteJobFile = path
    Exit Function
EH:
    Err.Raise Err.Number, "WriteJobFile", Err.Description
End Function


Private Sub WriteUtf8(ByVal path As String, ByVal text As String)
    On Error GoTo EH
    Dim stream As Object
    Set stream = CreateObject("ADODB.Stream")
    stream.Type = 2
    stream.Charset = "utf-8"
    stream.Open
    stream.WriteText text
    stream.SaveToFile path, 2
    stream.Close
    Exit Sub
EH:
    Err.Raise Err.Number, "WriteUtf8", Err.Description
End Sub


Private Sub EnsureFolder(ByVal folder As String)
    On Error GoTo EH
    Dim fso As Object, parent As String
    Set fso = CreateObject("Scripting.FileSystemObject")
    If fso.FolderExists(folder) Then Exit Sub
    parent = fso.GetParentFolderName(folder)
    If Len(parent) > 0 Then
        If Not fso.FolderExists(parent) Then EnsureFolder parent
    End If
    fso.CreateFolder folder
    Exit Sub
EH:
    Err.Raise Err.Number, "EnsureFolder", Err.Description
End Sub


Private Function RegexMatch(ByVal text As String, ByVal pattern As String) As Boolean
    On Error GoTo EH
    Dim rx As Object
    Set rx = CreateObject("VBScript.RegExp")
    rx.Pattern = pattern
    rx.IgnoreCase = True
    RegexMatch = rx.Test(text)
    Exit Function
EH:
    WriteIntakeLog "ERR invalid regex '" & pattern & "': " & Err.Description
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
    On Error GoTo EH
    Dim fso As Object, f As Object, folder As String
    Set fso = CreateObject("Scripting.FileSystemObject")
    folder = fso.GetParentFolderName(INTAKE_LOG)
    If Len(folder) > 0 Then If Not fso.FolderExists(folder) Then fso.CreateFolder folder
    Set f = fso.OpenTextFile(INTAKE_LOG, 8, True)
    f.WriteLine Format(Now, "yyyy-mm-dd hh:nn:ss") & " | " & t
    f.Close
    Exit Sub
EH:
    Debug.Print "Logging failed " & Err.Number & ": " & Err.Description & " | " & t
End Sub
'''

    event_code = f'''Private Sub Application_Startup()
    InitializeInboxWatcher
End Sub

Public Sub InitializeInboxWatcher()
    On Error GoTo EH
    Dim ns As Outlook.NameSpace
    Set ns = Application.GetNamespace("MAPI")
    Set inboxItems = ns.GetDefaultFolder(olFolderInbox).Items
    Debug.Print "Inbox watcher initialized: " & Now
    Exit Sub
EH:
    Debug.Print "Initialization error " & Err.Number & ": " & Err.Description
End Sub

Private Sub inboxItems_ItemAdd(ByVal Item As Object)
    On Error GoTo EH
    If Item Is Nothing Then Exit Sub
    If TypeOf Item Is Outlook.MailItem Then
        {'RouteMail Item' if all_in_one else 'SarthiDirectReceiver.RouteMail Item'}
    End If
    Exit Sub
EH:
    Debug.Print "ItemAdd error " & Err.Number & ": " & Err.Description
End Sub'''

    if all_in_one:
        footer = "\n\n' Outlook event wiring\n" + event_code
    else:
        commented = "\n".join("'   " + line if line else "'" for line in event_code.splitlines())
        footer = f'''

'====================================================================
'  Import this file as the SarthiDirectReceiver standard module.
'  Paste the commented block below into ThisOutlookSession, remove the
'  leading apostrophes, then run InitializeInboxWatcher once. Never run
'  inboxItems_ItemAdd manually; use TestSelectedEmail instead.
'====================================================================
'
'   Private WithEvents inboxItems As Outlook.Items
'
{commented}
'
'===================================================================='''

    full = header + "\n".join(watchers) + core + footer
    return _ascii_safe(full)


# VBA's parser is ASCII. A smart quote, em-dash, or any non-ASCII char from a
# feed name or rule value throws a "Syntax error" at line 1 and parks the cursor
# at the top — exactly the failure that's easy to misread as a paste problem.
# Fold the common typographic characters to ASCII, drop anything else.
_ASCII_MAP = {
    "\u2014": "--", "\u2013": "-", "\u2018": "'", "\u2019": "'",
    "\u201c": '"', "\u201d": '"', "\u2026": "...", "\u00a0": " ",
    "\u2022": "*", "\u2192": "->",
}


def _ascii_safe(text: str) -> str:
    for uni, asc in _ASCII_MAP.items():
        text = text.replace(uni, asc)
    return text.encode("ascii", "ignore").decode("ascii")


if __name__ == "__main__":
    print(generate())
