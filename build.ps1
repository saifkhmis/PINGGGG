# Builds dist\SmartPingStabilizer.exe and installer_out\SmartPingStabilizer-Setup.exe on Windows 11 x64.
# Needs: Python 3.11/3.12 x64 (python.org, tick "tcl/tk"), and Inno Setup 6 (https://jrsoftware.org/isdl.php) for the installer.
$ErrorActionPreference = "Stop"
python -m pip install -r requirements.txt
python -m unittest discover -s tests            # refuse to build if tests fail
python -m PyInstaller --noconfirm --onefile --windowed --uac-admin --name SmartPingStabilizer --collect-submodules pystray app.py
$iscc = "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
if (-not (Test-Path $iscc)) { $iscc = "$env:ProgramFiles\Inno Setup 6\ISCC.exe" }
if (Test-Path $iscc) { & $iscc installer.iss; Write-Host "Installer: installer_out\SmartPingStabilizer-Setup.exe" }
else { Write-Warning "Inno Setup not found - only dist\SmartPingStabilizer.exe was built." }
