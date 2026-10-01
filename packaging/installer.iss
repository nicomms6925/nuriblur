; NuriBlur Inno Setup 스크립트 (G3-05)
;
;   온라인(B2C):  iscc packaging\installer.iss
;   폐쇄망(기관):  iscc /DOFFLINE packaging\installer.iss
;
; 폐쇄망 설치본: URL 다운로드(yt-dlp)·업데이트 확인·모델 다운로드 3종을 모두 끈다(NURIBLUR_EDITION=org,
; NURIBLUR_OFFLINE=1). 모델은 번들에 포함(dist\NuriBlur\models). 라이선스는 사이트 키/USB(V1).

#define AppName "NuriBlur"
#define AppVersion "0.1.0"
#ifdef OFFLINE
  #define Edition "org"
  #define Suffix "-offline"
#else
  #define Edition "b2c"
  #define Suffix ""
#endif

[Setup]
AppId={{6B0E6C2A-5D1B-4D7E-9A57-0E7A57A1FA97}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=NuriBlur
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
OutputBaseFilename=NuriBlur-{#AppVersion}{#Suffix}-setup
Compression=lzma2/ultra64
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; 설치 경로 권한: Program Files(일반 사용자 쓰기 불가) — GS인증 점검 대비
DisableProgramGroupPage=yes
WizardStyle=modern
LicenseFile=..\docs\09-LICENSE-COMPLIANCE.md

[Languages]
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"

[Files]
Source: "..\dist\NuriBlur\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Dirs]
; 기관 DB(org.sqlite)는 설치 단위로 ProgramData에. 사용자는 읽기/쓰기, 감사 로그 무결성은 해시 체인+트리거로 보장
Name: "{commonappdata}\NuriBlur"; Permissions: users-modify

[Registry]
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: string; ValueName: "NURIBLUR_EDITION"; ValueData: "{#Edition}"; Flags: uninsdeletevalue
#ifdef OFFLINE
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; ValueType: string; ValueName: "NURIBLUR_OFFLINE"; ValueData: "1"; Flags: uninsdeletevalue
#endif

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\NuriBlur.exe"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\NuriBlur.exe"

[Run]
Filename: "{app}\NuriBlur.exe"; Description: "NuriBlur 실행"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 감사 로그·보고서는 기관 보존 대상이므로 제거 시 삭제하지 않는다 (org.sqlite 유지)
Type: filesandordirs; Name: "{localappdata}\NuriBlur\encoders.json"
