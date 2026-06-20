@echo off
REM Launch Caption Studio from the virtual environment.
REM Optional: pass a video path, e.g. run.bat C:\clips\myclip.mp4
setlocal
cd /d "%~dp0"
venv\Scripts\python.exe caption_studio.py %*
