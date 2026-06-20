# Create a virtual environment and install Caption Studio's dependencies.
$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

python -m venv venv
& .\venv\Scripts\python.exe -m pip install --upgrade pip
& .\venv\Scripts\python.exe -m pip install -r requirements.txt

Write-Host ""
Write-Host "Setup complete. Run the app with: .\run.ps1"
