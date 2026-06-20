@echo off
REM Build a standalone Caption Studio folder with PyInstaller (onedir).
REM Output: dist\CaptionStudio\CaptionStudio.exe
setlocal
cd /d "%~dp0"

if not exist venv\Scripts\python.exe (
  echo venv not found. Run setup.bat first.
  exit /b 1
)

venv\Scripts\python.exe -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error

venv\Scripts\python.exe -m PyInstaller --noconfirm caption_studio.spec
if errorlevel 1 goto :error

echo.
echo Built dist\CaptionStudio\  (zip and share that folder)
exit /b 0

:error
echo.
echo Build failed. See the messages above.
exit /b 1
