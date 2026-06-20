# Build a standalone Caption Studio folder with PyInstaller (onedir).
# Output: dist\CaptionStudio\CaptionStudio.exe
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

if (-not (Test-Path .\venv\Scripts\python.exe)) {
    Write-Host "venv not found. Run .\setup.ps1 first."
    exit 1
}

& .\venv\Scripts\python.exe -m pip install --upgrade pyinstaller
& .\venv\Scripts\python.exe -m PyInstaller --noconfirm caption_studio.spec

Write-Host ""
Write-Host "Built dist\CaptionStudio\  (zip and share that folder)"
