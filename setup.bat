@echo off
REM Create a virtual environment and install Caption Studio's dependencies.
setlocal
cd /d "%~dp0"

python -m venv venv
if errorlevel 1 goto :error

venv\Scripts\python.exe -m pip install --upgrade pip
if errorlevel 1 goto :error

venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto :error

echo.
echo Setup complete. Run the app with: run.bat
exit /b 0

:error
echo.
echo Setup failed. See the messages above.
exit /b 1
