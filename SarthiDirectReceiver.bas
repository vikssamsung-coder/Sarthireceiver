Attribute VB_Name = "SarthiDirectReceiver"
'====================================================================
'  Sarthi Direct Receiver  --  EVENT-DRIVEN INTAKE (queue architecture)
'
'  Outlook fires ItemAdd the instant a mail lands. This module saves
'  the attachment and drops a JOB on the processor's intake queue by
'  calling  run_direct.py --enqueue . It returns immediately -- Outlook
'  is NEVER blocked on processing, and there is no second Outlook COM
'  server to collide with the MIS mailer. The app's intake_worker
'  drains the queue and runs each dump flow.
'
'  ---- SETUP (once) -------------------------------------------------
'  1. Alt+F11 (Outlook VBA editor) -> import this file.
'  2. Edit the three constants below if your paths differ.
'  3. In ThisOutlookSession, wire the inbox event (copy the two
'     routines from the bottom of this file INTO ThisOutlookSession).
'  4. Tools > References -> ensure "Microsoft Outlook nn.n Object Library".
'  5. Macro security: File > Options > Trust Center > Macro Settings ->
'     enable macros / trust; restart Outlook.
'
'  TWO ways to route a mail (from the ItemAdd handler):
'     HandleDirectMail Item          -> app resolves type from your rules.
'     HandleDirectMailAs Item, "key" -> you name the dump type.
'====================================================================
Option Explicit

' ---- edit these if your paths differ -------------------------------
Private Const DROP_FOLDER As String = "D:\Sarthi\Incoming\"
Private Const RUNNER      As String = "D:\dump_processor_app\run_direct.py"
Private Const PYTHON      As String = "python"      ' or full path to python.exe
' --------------------------------------------------------------------


' Resolve the real SMTP address. Exchange senders otherwise arrive as an
' X.500 DN (/o=.../cn=...), which won't match "sender is one of a@bigul.co".
Public Function SenderSMTP(ByVal mail As Outlook.MailItem) As String
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


' Recognition-driven: the app figures out the type from your rules.
Public Sub HandleDirectMail(ByVal Item As Outlook.MailItem)
    RouteMail Item, ""
End Sub


' Explicit: you name the dump type key.
Public Sub HandleDirectMailAs(ByVal Item As Outlook.MailItem, ByVal dumpType As String)
    RouteMail Item, dumpType
End Sub


Private Sub RouteMail(ByVal Item As Outlook.MailItem, ByVal dumpType As String)
    On Error GoTo done
    Dim att As Outlook.Attachment, fpath As String, cmd As String
    Dim smtp As String, entryId As String

    If Item.Class <> olMail Then Exit Sub
    If Item.Attachments.Count = 0 Then Exit Sub
    If Len(Dir(DROP_FOLDER, vbDirectory)) = 0 Then MkDir DROP_FOLDER

    smtp = SenderSMTP(Item)
    entryId = Item.EntryID           ' stable id -> the queue dedupes on this

    For Each att In Item.Attachments
        fpath = DROP_FOLDER & att.FileName
        att.SaveAsFile fpath

        ' --enqueue: drop the job and return. The app's worker processes it.
        cmd = PYTHON & " """ & RUNNER & """ --enqueue " & _
              "--file """ & fpath & """ " & _
              "--subject """ & Replace(Item.Subject, """", "'") & """ " & _
              "--sender """ & smtp & """ " & _
              "--entry-id """ & entryId & """"
        If Len(dumpType) > 0 Then cmd = cmd & " --dump-type " & dumpType

        Shell "cmd /c " & cmd, vbHide
    Next
done:
End Sub


'====================================================================
'  COPY THE TWO ROUTINES BELOW INTO  ThisOutlookSession  (not here).
'  They subscribe to the Inbox and fire HandleDirectMail on every new
'  mail. To route only SOME mail, add an If on sender/subject before
'  the HandleDirectMail call.
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
'       On Error Resume Next
'       If TypeOf Item Is Outlook.MailItem Then
'           SarthiDirectReceiver.HandleDirectMail Item
'       End If
'   End Sub
'
'====================================================================
