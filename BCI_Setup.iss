#define MyAppName "Bannerlord Compatibility Intelligence"
#ifndef MyAppVersion
  #define MyAppVersion "0.5.2-alpha"
#endif
#define MyAppPublisher "BCI Project"
#define MyAppExeName "BCI.exe"

[Setup]
AppId={{E2A0D7CE-5D44-46E7-90CB-5DBA3B8B2F41}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppComments=Updates are delivered through official GitHub Releases.
DefaultDirName={autopf}\BCI
UsePreviousAppDir=yes
DefaultGroupName=BCI
DisableProgramGroupPage=yes
PrivilegesRequired=admin
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
MinVersion=10.0
OutputDir=dist
OutputBaseFilename=BCI_Setup_{#MyAppVersion}_x64
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
UninstallDisplayName={#MyAppName}
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no
CloseApplicationsFilter=BCI.exe

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Дополнительные ярлыки:"; Flags: unchecked

[Files]
; Keep the temporary prerequisite first so ExtractTemporaryFile does not decompress the application payload first.
Source: "redist\vc_redist.x64.exe"; Flags: dontcopy noencryption
Source: "dist\BCI\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\BCI"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\BCI"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить BCI"; Flags: nowait runasoriginaluser

; User data lives in %LOCALAPPDATA%\BCI and is intentionally not removed by uninstall.

[UninstallDelete]
Type: files; Name: "{app}\.bci-installed"

[Code]
const
  VCRedistRegistryKey = 'SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64';

function IsVCRedistAtLeastBundled: Boolean;
var
  FileMajor, FileMinor, FileRevision, FileBuild: Word;
  Installed, RegMajor, RegMinor, RegBuild, RegRevision: Cardinal;
begin
  Result := False;
  if not RegQueryDWordValue(HKLM64, VCRedistRegistryKey, 'Installed', Installed) then Exit;
  if Installed <> 1 then Exit;
  if not GetVersionComponents(ExpandConstant('{tmp}\vc_redist.x64.exe'),
    FileMajor, FileMinor, FileRevision, FileBuild) then Exit;
  if not RegQueryDWordValue(HKLM64, VCRedistRegistryKey, 'Major', RegMajor) then Exit;
  if not RegQueryDWordValue(HKLM64, VCRedistRegistryKey, 'Minor', RegMinor) then Exit;
  if not RegQueryDWordValue(HKLM64, VCRedistRegistryKey, 'Bld', RegBuild) then Exit;
  if not RegQueryDWordValue(HKLM64, VCRedistRegistryKey, 'Rbld', RegRevision) then Exit;

  if RegMajor <> FileMajor then begin Result := RegMajor > FileMajor; Exit; end;
  if RegMinor <> FileMinor then begin Result := RegMinor > FileMinor; Exit; end;
  if RegBuild <> FileRevision then begin Result := RegBuild > FileRevision; Exit; end;
  Result := RegRevision >= FileBuild;
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Result := '';
  ResultCode := -1;
  ExtractTemporaryFile('vc_redist.x64.exe');
  if IsVCRedistAtLeastBundled() then Exit;

  if not Exec(ExpandConstant('{tmp}\vc_redist.x64.exe'),
    '/install /quiet /norestart', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) then
  begin
    Result := 'Не удалось запустить установщик Microsoft Visual C++ Redistributable. Код ошибки Windows: ' + IntToStr(ResultCode);
    Exit;
  end;

  if ResultCode = 3010 then
  begin
    NeedsRestart := True;
    Result := 'Для завершения установки Microsoft Visual C++ требуется перезагрузка Windows. Перезагрузите компьютер и запустите установщик BCI ещё раз.';
    Exit;
  end;

  if ResultCode <> 0 then
  begin
    Result := 'Microsoft Visual C++ Redistributable завершился с кодом ' + IntToStr(ResultCode) + '. Установка BCI остановлена.';
    Exit;
  end;

  if not IsVCRedistAtLeastBundled() then
    Result := 'Не удалось подтвердить наличие требуемой версии Microsoft Visual C++ Redistributable. Установка BCI остановлена.';
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SaveStringToFile(ExpandConstant('{app}\.bci-installed'), 'Installed by BCI Setup' + #13#10, False);
end;
