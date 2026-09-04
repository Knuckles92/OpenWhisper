; Inno Setup script for OpenWhisper
;
; Build via scripts\build_installer.ps1, which passes AppVersion on the
; command line so the version stays owned by _version.py. To compile by hand:
;
;   ISCC.exe /DAppVersion=2.1.1 installer\OpenWhisper.iss
;
; Per-user install: no UAC prompt, no admin rights. The app only needs a
; low-level keyboard hook, which runs unelevated.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "OpenWhisper"
#define AppPublisher "Fiori Labs"
#define AppURL "https://openwhisper.fiorilabs.tech"
#define AppExeName "OpenWhisper.exe"
#define SourceDir "..\dist\OpenWhisper"

[Setup]
; AppId uniquely identifies this application for upgrades and uninstall.
; NEVER change it: a new GUID makes Windows treat an upgrade as a second,
; separate installation.
AppId={{CA36AD0A-13B9-4737-87AD-ADB54A28EFC9}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}
AppUpdatesURL={#AppURL}
VersionInfoVersion={#AppVersion}

; Per-user installation into %LOCALAPPDATA%\Programs\OpenWhisper.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayName={#AppName}
UninstallDisplayIcon={app}\{#AppExeName}

ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0

LicenseFile=..\LICENSE
OutputDir=Output
OutputBaseFilename={#AppName}-Setup-{#AppVersion}
SetupIconFile=..\ui_qt\assets\openwhisper.ico
WizardStyle=modern
; Solid compression is a large win here: the payload is ~300 MB of DLLs and
; Python bytecode with a lot of cross-file redundancy.
;
; lzma2/max (32 MB dictionary) rather than ultra64 (64 MB): ISCC.exe is a
; 32-bit process, and ultra64 combined with multiple block threads exhausts
; its address space on a payload this size and aborts with "Out of memory".
; The size difference between max and ultra64 here is under 1%.
Compression=lzma2/max
SolidCompression=yes
LZMANumBlockThreads=1
ShowLanguageDialog=no
DisableWelcomePage=no
; force, not yes: Restart Manager's graceful shutdown sends a close request,
; and the app answers it by minimizing to tray, so a plain `yes` ends in
; "Setup was unable to automatically close all applications". Builds up to
; 2.4.1 also fail to quit after handing off to an update, and this installer
; is their only way out.
CloseApplications=force
CloseApplicationsFilter=*.exe,*.dll
; Shared with the native updater so setup/uninstall wait for the app and helper.
AppMutex=OpenWhisper-App-CA36AD0A-13B9-4737-87AD-ADB54A28EFC9,Global\OpenWhisper-App-CA36AD0A-13B9-4737-87AD-ADB54A28EFC9,OpenWhisper-Update-CA36AD0A-13B9-4737-87AD-ADB54A28EFC9,Global\OpenWhisper-Update-CA36AD0A-13B9-4737-87AD-ADB54A28EFC9
SetupMutex=Global\OpenWhisper-Setup-CA36AD0A-13B9-4737-87AD-ADB54A28EFC9

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional shortcuts:"
Name: "startupicon"; Description: "Start {#AppName} when I sign in"; GroupDescription: "Startup:"; Flags: unchecked

[Files]
Source: "{#SourceDir}\OpenWhisperUpdater.exe"; Flags: dontcopy
Source: "{#SourceDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon
Name: "{userstartup}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: startupicon

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"
Type: files; Name: "{app}\{#AppExeName}"
Type: files; Name: "{app}\OpenWhisperUpdater.exe"
Type: files; Name: "{app}\.openwhisper-update.json"
Type: files; Name: "{app}\.openwhisper-update-complete"
Type: filesandordirs; Name: "{localappdata}\{#AppName}\updates"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Launch {#AppName}"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExeName}"; Flags: nowait runasoriginaluser; Check: IsInAppUpdate

[Code]
var
  SetupPrepared: Boolean;
  SetupFinished: Boolean;

function IsInAppUpdate(): Boolean;
begin
  Result := ExpandConstant('{param:OPENWHISPERUPDATE|0}') = '1';
end;

function UserDataDir(): String;
begin
  Result := ExpandConstant('{localappdata}\{#AppName}');
end;

function IsExistingInstall(): Boolean;
var
  UninstallKey: String;
begin
  UninstallKey := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
    '{CA36AD0A-13B9-4737-87AD-ADB54A28EFC9}_is1';
  Result := RegKeyExists(HKEY_CURRENT_USER, UninstallKey) or
    RegKeyExists(HKEY_LOCAL_MACHINE, UninstallKey);
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := IsExistingInstall() and
    ((PageID = wpWelcome) or
     (PageID = wpLicense) or
     (PageID = wpSelectDir) or
     (PageID = wpSelectProgramGroup) or
     (PageID = wpSelectTasks));
end;

{
  Belt and braces for CloseApplications=force, which is reported to leave some
  processes running. Setup is the recovery path for a build that cannot quit
  itself, so the running exe has to be gone before the first file is replaced,
  not "probably gone". No elevation is needed: the install is per-user and so
  is the process being closed.
}
procedure CloseRunningApp();
var
  ResultCode: Integer;
begin
  Exec(
    ExpandConstant('{sys}\taskkill.exe'),
    '/f /im "{#AppExeName}"',
    '',
    SW_HIDE,
    ewWaitUntilTerminated,
    ResultCode
  );
  { taskkill returns 128 when nothing matched, which is the normal case. }
  if ResultCode = 0 then
    Sleep(500);
end;

function RunSetupHelper(Action: String): String;
var
  ResultCode: Integer;
  ErrorFile: String;
  ErrorText: AnsiString;
begin
  ErrorFile := ExpandConstant('{tmp}\openwhisper-setup-error.txt');
  DeleteFile(ErrorFile);
  Result := '';
  if not Exec(
    ExpandConstant('{tmp}\OpenWhisperUpdater.exe'),
    '--setup-action ' + Action + ' --app-dir "' + ExpandConstant('{app}') +
      '" --error-file "' + ErrorFile + '"',
    ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, ResultCode
  ) then
    Result := 'Could not start the update helper. Please retry Setup.'
  else if ResultCode <> 0 then
  begin
    if LoadStringFromFile(ErrorFile, ErrorText) then
      Result := String(ErrorText)
    else
      Result := 'The update could not finish. Please retry Setup.';
  end;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if SetupPrepared then
  begin
    Result := '';
    Exit;
  end;
  CloseRunningApp();
  ExtractTemporaryFile('OpenWhisperUpdater.exe');
  Result := RunSetupHelper('prepare');
  SetupPrepared := Result = '';
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  ErrorText: String;
begin
  if CurStep = ssPostInstall then
  begin
    ErrorText := RunSetupHelper('finish');
    if ErrorText <> '' then
      RaiseException(ErrorText);
    SetupFinished := True;
  end;
end;

procedure DeinitializeSetup();
var
  ErrorText: String;
begin
  if SetupPrepared and not SetupFinished then
  begin
    ErrorText := RunSetupHelper('rollback');
    if ErrorText <> '' then
      MsgBox('Setup could not restore the previous version. Run Setup again.' + #13#10 +
        ErrorText, mbError, MB_OK);
  end;
end;

{
  Uninstall: offer to delete user data.

  Settings, history database, saved recordings, and any downloaded components
  live in %LOCALAPPDATA%\OpenWhisper, outside the install directory, so they
  survive an uninstall unless the user opts in here. Left unchecked by default
  because a reinstall should keep the user's transcription history.

  The Hugging Face model cache is deliberately NOT touched: it lives under
  ~\.cache\huggingface\hub and may be shared with other tools.
}
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  DataDir: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    DataDir := UserDataDir();
    if DirExists(DataDir) then
    begin
      if SuppressibleMsgBox(
           'Also remove your OpenWhisper settings, transcription history, and any' + #13#10 +
           'downloaded components?' + #13#10 + #13#10 +
           DataDir + #13#10 + #13#10 +
           'Choose No to keep them for a future reinstall.' + #13#10 + #13#10 +
           'Downloaded speech models are stored separately and are never removed.',
           mbConfirmation, MB_YESNO, IDNO) = IDYES then
      begin
        DelTree(DataDir, True, True, True);
      end;
    end;
  end;
end;
