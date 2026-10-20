@echo off
rem KL Unreal Box launcher (keep this file ASCII-only, cmd reads it as ANSI/GBK)
cd /d "%~dp0"
if not exist "Engine\Scripts\pythonw.exe" (
    echo [KL Box] engine missing. Run install.bat first.
    pause
    exit /b 1
)
start "" "Engine\Scripts\pythonw.exe" "UEMBox\app\UEMBox.py"
