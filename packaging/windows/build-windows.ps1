param([string]$Version = "0.1.0-beta")
$ErrorActionPreference = "Stop"
$DIST = "dist\wrekker-windows"
$PYTHON_URL = "https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip"

New-Item -ItemType Directory -Force -Path $DIST | Out-Null
Invoke-WebRequest $PYTHON_URL -OutFile python-embed.zip
Expand-Archive python-embed.zip -DestinationPath "$DIST\python" -Force

Invoke-WebRequest "https://bootstrap.pypa.io/get-pip.py" -OutFile get-pip.py
& "$DIST\python\python.exe" get-pip.py
(Get-Content "$DIST\python\python311._pth") -replace "#import site", "import site" |
  Set-Content "$DIST\python\python311._pth"

$PYTHON = Resolve-Path "$DIST\python\python.exe"
& $PYTHON -m pip install PyQt6 librosa soundfile numpy scipy sounddevice mutagen mido python-rtmidi structlog rich pyinstaller maturin

$WHEELS = "$PSScriptRoot\..\..\dist\wrekker-wheels"
New-Item -ItemType Directory -Force -Path $WHEELS | Out-Null
Push-Location wrekker\engine_rs
& $PYTHON -m maturin build --release --target x86_64-pc-windows-msvc --out $WHEELS
Pop-Location
& $PYTHON -m pip install --no-deps (Get-Item "$WHEELS\wrekker_engine-*.whl").FullName

Invoke-WebRequest "https://breakfastquay.com/files/releases/rubberband-3.3.0-gpl-executable-windows.zip" `
  -OutFile rubberband-windows.zip
Expand-Archive rubberband-windows.zip -DestinationPath rubberband-tmp -Force
Copy-Item "rubberband-tmp\rubberband.dll" "$DIST\" -Force

Invoke-WebRequest "https://aka.ms/vs/17/release/vc_redist.x64.exe" -OutFile "$DIST\vcredist_x64.exe"

Copy-Item -Recurse wrekker "$DIST\python\Lib\site-packages\" -Force

& "$DIST\python\python.exe" -m PyInstaller `
    --onefile --windowed --name wrekker `
    --icon assets\icon.ico `
    --add-data "wrekker;wrekker" `
    --hidden-import wrekker.ui.app `
    packaging\windows\launcher.py
Copy-Item "dist\wrekker.exe" "$DIST\" -Force

makensis /DVERSION=$Version packaging\windows\installer.nsi
