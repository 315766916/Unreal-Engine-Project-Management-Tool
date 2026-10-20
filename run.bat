@echo off
启动 "" "Engine\Scripts\pythonw.exe" "UEMBox\app\UEMBox.py"
cd /d "%~dp0"
if not exist "Engine\Scripts\pythonw.exe" (
    echo [KL Box] engine missing. Run install.bat first.
    pause
    exit /b 1
)
start "" "Engine\Scripts\pythonw.exe" "UEMBox\app\UEMBox.py"
