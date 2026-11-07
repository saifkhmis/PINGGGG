; Inno Setup 6 script. Build with:  iscc installer.iss   (after build.ps1 has produced dist\SmartPingStabilizer.exe)
[Setup]
AppName=Smart Ping Stabilizer
AppVersion=1.0.0
AppPublisher=Smart Ping Stabilizer
DefaultDirName={autopf}\SmartPingStabilizer
DefaultGroupName=Smart Ping Stabilizer
OutputDir=installer_out
OutputBaseFilename=SmartPingStabilizer-Setup
Compression=lzma2
SolidCompression=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
UninstallDisplayIcon={app}\SmartPingStabilizer.exe

[Files]
Source: "dist\SmartPingStabilizer.exe"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Smart Ping Stabilizer"; Filename: "{app}\SmartPingStabilizer.exe"
Name: "{group}\Emergency Restore Networking"; Filename: "{app}\SmartPingStabilizer.exe"; Parameters: "--restore"
Name: "{autodesktop}\Smart Ping Stabilizer"; Filename: "{app}\SmartPingStabilizer.exe"

[Run]
; autostart at logon, elevated, WITHOUT a UAC prompt each time (task runs with highest privileges)
Filename: "schtasks.exe"; Parameters: "/Create /TN ""Smart Ping Stabilizer"" /TR ""\""{app}\SmartPingStabilizer.exe\"" --background"" /SC ONLOGON /RL HIGHEST /F"; Flags: runhidden; StatusMsg: "Registering autostart..."
Filename: "{app}\SmartPingStabilizer.exe"; Parameters: "--background"; Flags: nowait postinstall skipifsilent runascurrentuser; Description: "Start Smart Ping Stabilizer now"

[UninstallRun]
Filename: "{app}\SmartPingStabilizer.exe"; Parameters: "--restore"; Flags: runhidden; RunOnceId: "SPSRestore"
Filename: "schtasks.exe"; Parameters: "/Delete /TN ""Smart Ping Stabilizer"" /F"; Flags: runhidden; RunOnceId: "SPSTask"
Filename: "taskkill.exe"; Parameters: "/F /IM SmartPingStabilizer.exe"; Flags: runhidden; RunOnceId: "SPSKill"
