; STT_KOR - Inno Setup 설치 마법사
;
; PyInstaller onedir 산출물(dist\STT_KOR\)을 하나의 Setup.exe 로 묶는다.
; 목적: 비전공 사용자가 Python/pip/PATH/관리자권한 없이 더블클릭만으로
;       설치하고, 시작 메뉴·바탕화면 아이콘으로 실행하게 하는 것.
;
; 로컬 빌드:
;   1) powershell -ExecutionPolicy Bypass -File build_exe.ps1   (dist\STT_KOR 생성)
;   2) "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" /DAppVersion=1.0.3 installer.iss
;   -> installer_out\STT_KOR-v1.0.3-Setup.exe
;
; 릴리스 빌드는 .github/workflows/release.yml 가 자동으로 수행한다.

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

#define AppName "한국어 강의 STT"
#define AppExeName "STT_KOR.exe"
#define AppPublisher "NombarHwan"
#define AppURL "https://github.com/NombarHwan/stt_kor"

[Setup]
AppId={{8C9A4E2F-6D1B-4F3A-A05E-7B2C9D4E1F60}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
AppPublisherURL={#AppURL}
AppSupportURL={#AppURL}/issues
AppUpdatesURL={#AppURL}/releases
DefaultDirName={autopf}\STT_KOR
DefaultGroupName=STT_KOR
UninstallDisplayIcon={app}\{#AppExeName}
UninstallDisplayName={#AppName}
OutputDir=installer_out
OutputBaseFilename=STT_KOR-v{#AppVersion}-Setup
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
; 관리자 권한 요구 안 함 -> UAC 창이 안 뜨고, %LOCALAPPDATA%\Programs 에 설치된다.
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
; 비전공 사용자용: 선택 화면을 전부 없애고 더블클릭 -> 진행 -> 완료로 끝낸다.
DisableWelcomePage=yes
DisableDirPage=yes
DisableProgramGroupPage=yes
DisableReadyPage=yes
ShowLanguageDialog=no

[Languages]
Name: "default"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\STT_KOR\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\{#AppName} 제거"; Filename: "{uninstallexe}"
; 데스크톱 아이콘은 항상 생성한다(선택 화면 없이). 제거 시 함께 삭제됨.
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "지금 {#AppName} 실행"; Flags: nowait postinstall skipifsilent

[Messages]
; 기본 언어 파일은 영문(Default.isl). 사용자가 실제로 보게 되는 문구만 한국어로 교체한다.
SetupAppTitle=설치
SetupWindowTitle=%1 설치
WizardPreparing=설치 준비
PreparingDesc=[name] 을(를) 설치할 준비를 하고 있습니다.
WizardInstalling=설치 중
InstallingLabel=[name] 을(를) 설치하는 동안 잠시 기다려 주세요.
StatusCreateDirs=폴더를 만드는 중...
StatusExtractFiles=파일을 푸는 중...
StatusCreateIcons=바로 가기를 만드는 중...
FinishedHeadingLabel=[name] 설치 완료
FinishedLabel=[name] 설치가 끝났습니다. 바탕화면 또는 시작 메뉴의 아이콘으로 실행할 수 있습니다.
FinishedLabelNoIcons=[name] 설치가 끝났습니다.
ClickFinish=[마침] 을 누르면 설치가 종료됩니다.
ButtonFinish=마침(&F)
ButtonCancel=취소
RunEntryExec=%1 실행
ExitSetupTitle=설치 종료
ExitSetupMessage=설치가 아직 끝나지 않았습니다. 지금 종료하면 프로그램이 설치되지 않습니다.%n%n나중에 설치 파일을 다시 실행할 수 있습니다.%n%n설치를 종료할까요?
AboutSetupTitle=설치 정보
StatusUninstalling=제거하는 중...
ConfirmUninstall=%1 을(를) 정말로 제거할까요?
UninstallStatusLabel=%1 을(를) 제거하는 동안 잠시 기다려 주세요.
ButtonUninstall=제거(&U)
UninstalledAll=%1 이(가) 완전히 제거되었습니다.
