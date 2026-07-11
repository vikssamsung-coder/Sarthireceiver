Attribute VB_Name = "SarthiDirectReceiver"
'====================================================================
'  Sarthi Direct Receiver  (ADDITIVE MODULE)
'  Hands a caught email to the Dump Processor app for processing.
'
'  This module is self-contained. Import it and leave all your
'  existing watcher code exactly as it is. Nothing here touches or
'  replaces your other modules.
'
'  For a mail you want routed through the app, call ONE of:
'     SarthiDirectReceiver.HandleDirectMail Item
'         -> the app decides the dump type from your identifier
'            rules (sender / subject / body).
'     SarthiDirectReceiver.HandleDirectMailAs Item, "cube_calllog"
'         -> you name the dump type (most reliable for a bespoke
'            watcher that already knows what it caught).
'====================================================================
Option Explicit

' ---- edit these three if your paths differ -------------------------
Private Const DROP_FOLDER As String = "D:\Sarthi\Incoming\"
Private Const RUNNER      As String = "D:\dump_processor_app\run_direct.py"
Private Const PYTHON      As String = "python"      ' or full path to python.exe
' --------------------------------------------------------------------


' Resolve the real SMTP address. Exchange senders otherwise come through
' as an X.500 DN (/o=.../cn=...), which won't match your "sender is one of
' a@bigul.co" rules. (Not needed if you always call HandleDirectMailAs.)
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


' Explicit: you tell it the dump type key.
Public Sub HandleDirectMailAs(ByVal Item As Outlook.MailItem, ByVal dumpType As String)
    RouteMail Item, dumpType
End Sub


Private Sub RouteMail(ByVal Item As Outlook.MailItem, ByVal dumpType As String)
    On Error GoTo done
    Dim att As Outlook.Attachment, fpath As String, cmd As String, smtp As String

    If Item.Attachments.Count = 0 Then Exit Sub
    If Len(Dir(DROP_FOLDER, vbDirectory)) = 0 Then MkDir DROP_FOLDER
    smtp = SenderSMTP(Item)

    For Each att In Item.Attachments
        fpath = DROP_FOLDER & att.FileName
        att.SaveAsFile fpath

        cmd = PYTHON & " """ & RUNNER & """ " & _
              "--file """ & fpath & """ " & _
              "--subject """ & Replace(Item.Subject, """", "'") & """ " & _
              "--sender """ & smtp & """"
        If Len(dumpType) > 0 Then cmd = cmd & " --dump-type " & dumpType

        Shell "cmd /c " & cmd, vbHide
    Next
done:
End Sub
