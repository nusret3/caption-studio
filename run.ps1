# Launch Caption Studio from the virtual environment.
# Optional: pass a video path, e.g. .\run.ps1 C:\clips\myclip.mp4
Set-Location -Path $PSScriptRoot
& .\venv\Scripts\python.exe caption_studio.py @args
